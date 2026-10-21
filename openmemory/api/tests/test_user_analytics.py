"""Tests for /admin/analytics endpoints."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, DEFAULT_GROUP_NAME, Group, Machine, MachineStatus, User, WriteAuditLog, WriteQueueJob
from app.models import USER_TYPE_PERSON
from app.read_audit_log_model import ReadAuditLog
from app.routers.user_analytics import router


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


@pytest.fixture
def client(factory):
    app = FastAPI()
    app.include_router(router)

    def _override():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def _seed_group_and_user(factory, hostname="alice-pc", group_name="Dev"):
    s = factory()
    try:
        default = Group(name=DEFAULT_GROUP_NAME)
        team = Group(name=group_name)
        s.add_all([default, team])
        s.flush()
        user = User(user_id=hostname, group_id=team.id)
        s.add(user)
        s.commit()
        return str(team.id), hostname
    finally:
        s.close()


def _seed_audit(factory, hostname="alice-pc"):
    import uuid as _uuid

    s = factory()
    try:
        now = datetime.now(timezone.utc)
        job_id = _uuid.uuid4()
        s.add(
            WriteQueueJob(
                id=job_id,
                project="proj-a",
                hostname=hostname,
                client_name="cursor",
                text="User prefers dark mode in the IDE",
            )
        )
        s.add(
            WriteAuditLog(
                job_id=job_id,
                project="proj-a",
                hostname=hostname,
                client_name="cursor",
                action="enqueue",
                created_at=now - timedelta(hours=1),
            )
        )
        s.add(
            ReadAuditLog(
                project="proj-a",
                memory_id="mem-1",
                access_type="search",
                source="mcp",
                hostname=hostname,
                client_name="cursor",
                accessed_at=now - timedelta(hours=2),
            )
        )
        s.commit()
    finally:
        s.close()


def test_classify_presence_online_with_recent_activity():
    from app.models import get_current_utc_time
    from app.utils.user_analytics import classify_presence

    now = get_current_utc_time()
    level, days = classify_presence(
        writes_24h=1,
        reads_24h=0,
        last_write_at=now,
        last_read_at=None,
    )
    assert level == "online"
    assert days is None


def test_classify_presence_offline_after_24h():
    from app.models import get_current_utc_time
    from app.utils.user_analytics import classify_presence

    now = get_current_utc_time()
    last = now - timedelta(days=3)
    level, days = classify_presence(
        writes_24h=0,
        reads_24h=0,
        last_write_at=last,
        last_read_at=None,
        now=now,
    )
    assert level == "offline"
    assert days == 3


def test_classify_presence_offline_without_history():
    from app.utils.user_analytics import classify_presence

    level, days = classify_presence(
        writes_24h=0,
        reads_24h=0,
        last_write_at=None,
        last_read_at=None,
    )
    assert level == "offline"
    assert days is None


def test_classify_presence_offline_with_naive_db_timestamps():
    """Audit columns are timestamp without time zone; wall clock is aware UTC.

    Mixing them in offline-day arithmetic used to raise TypeError → HTTP 500 on
    GET /admin/analytics/groups/{id} when any member was offline.
    """
    from datetime import timezone

    from app.utils.user_analytics import classify_presence

    now = datetime.now(timezone.utc)
    last_naive = (now - timedelta(days=5)).replace(tzinfo=None)
    level, days = classify_presence(
        writes_24h=0,
        reads_24h=0,
        last_write_at=last_naive,
        last_read_at=None,
        now=now,
    )
    assert level == "offline"
    assert days == 5


def test_list_groups_analytics_empty(client):
    r = client.get("/admin/analytics/groups")
    assert r.status_code == 200
    assert r.json()["groups"] == []


def test_group_analytics_with_member_stats(factory, client):
    group_id, hostname = _seed_group_and_user(factory)
    _seed_audit(factory, hostname)
    r = client.get(f"/admin/analytics/groups/{group_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["group"]["name"] == "Dev"
    assert body["group"]["member_count"] == 1
    assert body["group"]["writes_total"] == 1
    assert body["group"]["reads_total"] == 1
    assert len(body["members"]) == 1
    assert body["members"][0]["user_id"] == hostname
    assert body["members"][0]["usage_level"] == "online"
    assert body["members"][0]["offline_days"] is None


def test_group_analytics_includes_person_account_and_linked_machine(factory, client):

    s = factory()
    try:
        group = Group(name="Google Team")
        s.add(group)
        s.flush()
        person = User(
            user_id="google-sub-1",
            user_type=USER_TYPE_PERSON,
            google_sub="google-sub-1",
            display_name="Rafael Chielle",
            group_id=group.id,
        )
        s.add(person)
        s.flush()
        s.add(
            Machine(
                hostname="S0352",
                linked_user_id=person.id,
                status=MachineStatus.linked,
            )
        )
        s.commit()
        group_id = str(group.id)
    finally:
        s.close()

    r = client.get(f"/admin/analytics/groups/{group_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["group"]["member_count"] == 1
    assert body["members"][0]["user_id"] == "S0352"
    assert body["members"][0]["display_name"] == "Rafael Chielle"


def test_group_analytics_offline_member_does_not_500(factory, client):
    """Regression: group detail must return 200 when a member is offline."""
    from datetime import timezone

    group_id, hostname = _seed_group_and_user(factory, hostname="offline-pc")
    s = factory()
    try:
        s.add(
            WriteAuditLog(
                project="proj-a",
                hostname=hostname,
                client_name="cursor",
                action="enqueue",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=4),
            )
        )
        s.commit()
    finally:
        s.close()

    r = client.get(f"/admin/analytics/groups/{group_id}")
    assert r.status_code == 200
    member = r.json()["members"][0]
    assert member["user_id"] == hostname
    assert member["usage_level"] == "offline"
    assert member["offline_days"] >= 4


def test_group_analytics_shows_google_display_name_for_linked_machine(factory, client):
    """Após o vínculo, a person (no mesmo grupo) é o membro canônico com o hostname."""
    import uuid as _uuid

    group_id, hostname = _seed_group_and_user(factory, hostname="S0293")
    s = factory()
    try:
        person = User(
            user_id="google-sub-1",
            google_sub="google-sub-1",
            display_name="João Silva",
            user_type=USER_TYPE_PERSON,
            group_id=_uuid.UUID(group_id),
        )
        legacy = s.query(User).filter(User.user_id == hostname).one()
        s.add(person)
        s.flush()
        s.add(
            Machine(
                hostname=hostname,
                linked_user_id=person.id,
                legacy_user_id=legacy.id,
                status=MachineStatus.linked,
            )
        )
        s.commit()
    finally:
        s.close()

    r = client.get(f"/admin/analytics/groups/{group_id}")
    assert r.status_code == 200
    members = r.json()["members"]
    assert len(members) == 1
    member = members[0]
    assert member["user_id"] == hostname
    assert member["display_name"] == "João Silva"


def test_user_analytics_detail(factory, client):
    _, hostname = _seed_group_and_user(factory)
    _seed_audit(factory, hostname)
    r = client.get(f"/admin/analytics/users/{hostname}")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == hostname
    assert body["writes_total"] == 1
    assert body["reads_total"] == 1
    assert len(body["recent_writes"]) == 1
    assert body["recent_writes"][0]["text_preview"] == "User prefers dark mode in the IDE"
    assert len(body["recent_reads"]) == 1


def test_user_analytics_unregistered_hostname(factory, client):
    _seed_audit(factory, "ghost-pc")
    r = client.get("/admin/analytics/users/ghost-pc")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "ghost-pc"
    assert body["writes_total"] == 1


def test_user_analytics_ui_hostname_reads(factory, client):
    """Reads from the UI are stored as ui:{hostname} — must count toward user stats."""
    _, hostname = _seed_group_and_user(factory, hostname="alice-pc")
    s = factory()
    try:
        now = datetime.now(timezone.utc)
        s.add(
            ReadAuditLog(
                project="proj-a",
                memory_id="mem-ui-1",
                access_type="search",
                source="api",
                hostname=f"ui:{hostname}",
                client_name="openmemory",
                accessed_at=now,
            )
        )
        s.commit()
    finally:
        s.close()

    r = client.get(f"/admin/analytics/users/{hostname}")
    assert r.status_code == 200
    body = r.json()
    assert body["reads_total"] == 1
    assert body["reads_24h"] == 1
    assert len(body["recent_reads"]) == 1


def test_analytics_overview(factory, client):
    group_id, hostname = _seed_group_and_user(factory)
    _seed_audit(factory, hostname)
    r = client.get("/admin/analytics/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["total_users"] == 1
    assert body["total_groups"] == 2  # Default + Dev
    assert body["writes_total"] >= 1
    assert body["writes_24h"] >= 1
    assert body["writes_7d"] >= 1
    assert body["reads_total"] >= 1
    assert body["reads_24h"] >= 1
    assert body["reads_7d"] >= 1


def test_analytics_overview_counts_visible_members_not_linked_legacy(factory, client):
    """Overview total must match sum of group member_count (person + legacy = 1)."""
    import uuid as _uuid

    group_id, hostname = _seed_group_and_user(factory, hostname="S0293")
    s = factory()
    try:
        person = User(
            user_id="google-sub-1",
            google_sub="google-sub-1",
            display_name="João Silva",
            user_type=USER_TYPE_PERSON,
            group_id=_uuid.UUID(group_id),
        )
        legacy = s.query(User).filter(User.user_id == hostname).one()
        s.add(person)
        s.flush()
        s.add(
            Machine(
                hostname=hostname,
                linked_user_id=person.id,
                legacy_user_id=legacy.id,
                status=MachineStatus.linked,
            )
        )
        s.commit()
    finally:
        s.close()

    overview = client.get("/admin/analytics/overview").json()
    groups = client.get("/admin/analytics/groups").json()["groups"]
    member_sum = sum(group["member_count"] for group in groups)

    assert overview["total_users"] == 1
    assert member_sum == 1


def test_top_contributors_empty(client):
    r = client.get("/admin/analytics/top-contributors")
    assert r.status_code == 200
    body = r.json()
    assert body["metric"] == "total"
    assert body["period"] == "7d"
    assert body["items"] == []


def _seed_multi_user_audit(factory):
    s = factory()
    try:
        default = Group(name=DEFAULT_GROUP_NAME)
        team = Group(name="Dev")
        s.add_all([default, team])
        s.flush()
        alice = User(user_id="alice-pc", group_id=team.id)
        bob = User(user_id="bob-pc", group_id=team.id)
        s.add_all([alice, bob])
        s.commit()
        group_id = str(team.id)
    finally:
        s.close()

    now = datetime.now(timezone.utc)
    s = factory()
    try:
        for i in range(3):
            s.add(
                WriteAuditLog(
                    project="proj-a",
                    hostname="alice-pc",
                    client_name="cursor",
                    action="enqueue",
                    created_at=now - timedelta(hours=i),
                )
            )
        s.add(
            WriteAuditLog(
                project="proj-b",
                hostname="bob-pc",
                client_name="cursor",
                action="enqueue",
                created_at=now - timedelta(hours=1),
            )
        )
        s.add(
            ReadAuditLog(
                project="proj-a",
                memory_id="mem-1",
                access_type="search",
                source="mcp",
                hostname="bob-pc",
                client_name="cursor",
                accessed_at=now - timedelta(hours=1),
            )
        )
        s.add(
            ReadAuditLog(
                project="proj-a",
                memory_id="mem-ui-1",
                access_type="search",
                source="api",
                hostname="ui:alice-pc",
                client_name="openmemory",
                accessed_at=now,
            )
        )
        s.commit()
    finally:
        s.close()
    return group_id


def test_top_contributors_ranks_by_total(factory, client):
    _seed_multi_user_audit(factory)
    r = client.get("/admin/analytics/top-contributors?metric=total&period=all")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert items[0]["user_id"] == "alice-pc"
    assert items[0]["rank"] == 1
    assert items[0]["writes"] == 3
    assert items[0]["reads"] == 1
    assert items[0]["value"] == 100.0
    assert items[1]["user_id"] == "bob-pc"
    assert items[1]["value"] == pytest.approx(53.33)


def test_contributor_score_weights_writes_70_reads_30():
    from app.utils.user_analytics import contributor_score

    assert contributor_score(10, 0, max_writes=10, max_reads=50) == 70.0
    assert contributor_score(0, 50, max_writes=10, max_reads=50) == 30.0
    assert contributor_score(10, 50, max_writes=10, max_reads=50) == 100.0
    assert contributor_score(0, 0, max_writes=0, max_reads=0) == 0.0


def test_top_contributors_total_prioritizes_writes_over_reads(factory, client):
    s = factory()
    try:
        team = Group(name="Dev")
        s.add(team)
        s.flush()
        s.add_all(
            [
                User(user_id="writer-pc", group_id=team.id),
                User(user_id="reader-pc", group_id=team.id),
            ]
        )
        now = datetime.now(timezone.utc)
        for _ in range(18):
            s.add(
                WriteAuditLog(
                    project="proj-a",
                    hostname="writer-pc",
                    client_name="cursor",
                    action="enqueue",
                    created_at=now,
                )
            )
        s.add(
            WriteAuditLog(
                project="proj-a",
                hostname="reader-pc",
                client_name="cursor",
                action="enqueue",
                created_at=now,
            )
        )
        for i, (host, count) in enumerate((("writer-pc", 25), ("reader-pc", 50))):
            for j in range(count):
                s.add(
                    ReadAuditLog(
                        project="proj-a",
                        memory_id=f"mem-{i}-{j}",
                        access_type="search",
                        source="mcp",
                        hostname=host,
                        client_name="cursor",
                        accessed_at=now,
                    )
                )
        s.commit()
    finally:
        s.close()

    r = client.get("/admin/analytics/top-contributors?metric=total&period=all")
    items = r.json()["items"]
    assert [item["user_id"] for item in items] == ["writer-pc", "reader-pc"]
    assert items[0]["value"] == 85.0
    assert items[1]["value"] == pytest.approx(33.89)


def test_top_contributors_metric_writes(factory, client):
    _seed_multi_user_audit(factory)
    r = client.get("/admin/analytics/top-contributors?metric=writes&period=all")
    items = r.json()["items"]
    assert items[0]["user_id"] == "alice-pc"
    assert items[0]["value"] == 3
    assert items[1]["user_id"] == "bob-pc"
    assert items[1]["value"] == 1


def test_top_contributors_metric_reads(factory, client):
    _seed_multi_user_audit(factory)
    r = client.get("/admin/analytics/top-contributors?metric=reads&period=all")
    items = r.json()["items"]
    assert len(items) == 2
    assert {item["user_id"] for item in items} == {"alice-pc", "bob-pc"}
    assert all(item["value"] == 1 for item in items)


def test_top_contributors_filters_by_project(factory, client):
    _seed_multi_user_audit(factory)
    r = client.get(
        "/admin/analytics/top-contributors?metric=writes&period=all&project=proj-b"
    )
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["user_id"] == "bob-pc"
    assert items[0]["writes"] == 1


def test_top_contributors_filters_by_group(factory, client):
    group_id = _seed_multi_user_audit(factory)
    r = client.get(f"/admin/analytics/top-contributors?period=all&group_id={group_id}")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2


def test_top_contributors_unknown_group_404(factory, client):
    import uuid as _uuid

    r = client.get(f"/admin/analytics/top-contributors?group_id={_uuid.uuid4()}")
    assert r.status_code == 404


def test_top_contributors_excludes_system_users(factory, client):
    _seed_multi_user_audit(factory)
    s = factory()
    try:
        default = s.query(Group).filter(Group.name == DEFAULT_GROUP_NAME).one()
        s.add_all(
            [
                User(user_id="unknown-host", group_id=default.id),
                User(user_id="openmemory", group_id=default.id),
            ]
        )
        now = datetime.now(timezone.utc)
        for _ in range(50):
            s.add(
                WriteAuditLog(
                    project="proj-a",
                    hostname="unknown-host",
                    client_name="cursor",
                    action="enqueue",
                    created_at=now,
                )
            )
        for _ in range(40):
            s.add(
                WriteAuditLog(
                    project="proj-a",
                    hostname="openmemory",
                    client_name="openmemory-ui",
                    action="enqueue",
                    created_at=now,
                )
            )
        s.commit()
    finally:
        s.close()

    items = client.get("/admin/analytics/top-contributors?metric=writes&period=all").json()["items"]
    user_ids = [item["user_id"] for item in items]
    assert "unknown-host" not in user_ids
    assert "openmemory" not in user_ids
    assert "alice-pc" in user_ids


def test_top_contributors_excludes_default_group(factory, client):
    s = factory()
    try:
        default = Group(name=DEFAULT_GROUP_NAME)
        team = Group(name="Dev")
        s.add_all([default, team])
        s.flush()
        s.add(User(user_id="mini-pc", group_id=default.id))
        s.add(User(user_id="team-user", group_id=team.id))
        s.commit()
        team_id = str(team.id)
        default_id = str(default.id)
    finally:
        s.close()

    now = datetime.now(timezone.utc)
    s = factory()
    try:
        for _ in range(20):
            s.add(
                WriteAuditLog(
                    project="proj-a",
                    hostname="mini-pc",
                    client_name="cursor",
                    action="enqueue",
                    created_at=now,
                )
            )
        s.add(
            WriteAuditLog(
                project="proj-a",
                hostname="team-user",
                client_name="cursor",
                action="enqueue",
                created_at=now,
            )
        )
        s.commit()
    finally:
        s.close()

    all_items = client.get("/admin/analytics/top-contributors?metric=writes&period=all").json()["items"]
    assert [item["user_id"] for item in all_items] == ["team-user"]

    default_items = client.get(
        f"/admin/analytics/top-contributors?metric=writes&period=all&group_id={default_id}"
    ).json()["items"]
    assert default_items == []

    team_items = client.get(
        f"/admin/analytics/top-contributors?metric=writes&period=all&group_id={team_id}"
    ).json()["items"]
    assert team_items[0]["user_id"] == "team-user"


def test_top_contributors_dedupes_linked_legacy(factory, client):
    import uuid as _uuid

    group_id, hostname = _seed_group_and_user(factory, hostname="S0293")
    s = factory()
    try:
        person = User(
            user_id="google-sub-1",
            google_sub="google-sub-1",
            display_name="João Silva",
            user_type=USER_TYPE_PERSON,
            group_id=_uuid.UUID(group_id),
        )
        legacy = s.query(User).filter(User.user_id == hostname).one()
        s.add(person)
        s.flush()
        s.add(
            Machine(
                hostname=hostname,
                linked_user_id=person.id,
                legacy_user_id=legacy.id,
                status=MachineStatus.linked,
            )
        )
        now = datetime.now(timezone.utc)
        s.add(
            WriteAuditLog(
                project="proj-a",
                hostname=hostname,
                client_name="cursor",
                action="enqueue",
                created_at=now,
            )
        )
        s.commit()
    finally:
        s.close()

    r = client.get("/admin/analytics/top-contributors?metric=writes&period=all")
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["user_id"] == hostname
    assert items[0]["display_name"] == "João Silva"
    assert items[0]["writes"] == 1


def _delete_legacy_user(client, hostname: str, confirm: str):
    return client.request(
        "DELETE",
        f"/admin/analytics/users/{hostname}",
        content=json.dumps({"confirm": confirm}),
        headers={"Content-Type": "application/json"},
    )


def test_delete_legacy_user_requires_confirm(factory, client):
    hostname = "junk-host"
    _seed_group_and_user(factory, hostname=hostname)
    r = _delete_legacy_user(client, hostname, "wrong")
    assert r.status_code == 400


def test_delete_legacy_user_removes_sql_row(factory, client):
    hostname = "junk-host"
    _seed_group_and_user(factory, hostname=hostname)
    r = _delete_legacy_user(client, hostname, hostname)
    assert r.status_code == 200
    assert r.json()["status"] == "deleted"
    assert r.json()["qdrant_preserved"] is True

    s = factory()
    try:
        assert s.query(User).filter(User.user_id == hostname).first() is None
    finally:
        s.close()

    assert client.get(f"/admin/analytics/users/{hostname}").status_code == 200


def test_group_analytics_dedupes_case_insensitive_members(factory, client):
    s = factory()
    try:
        default = Group(name=DEFAULT_GROUP_NAME)
        team = Group(name="Dev")
        s.add_all([default, team])
        s.flush()
        s.add_all(
            [
                User(user_id="Hermes", group_id=team.id),
                User(user_id="hermes", group_id=team.id),
            ]
        )
        s.commit()
        group_id = str(team.id)
    finally:
        s.close()

    r = client.get(f"/admin/analytics/groups/{group_id}")
    assert r.status_code == 200
    members = r.json()["members"]
    assert len(members) == 1
    assert members[0]["user_id"] == "Hermes"


def test_delete_legacy_user_blocks_system_user(factory, client, monkeypatch):
    monkeypatch.setenv("USER", "openmemory")
    s = factory()
    try:
        g = Group(name=DEFAULT_GROUP_NAME)
        s.add(g)
        s.flush()
        s.add(User(user_id="openmemory", group_id=g.id))
        s.commit()
    finally:
        s.close()
    r = _delete_legacy_user(client, "openmemory", "openmemory")
    assert r.status_code == 403
