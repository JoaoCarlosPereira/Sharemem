"""Resolve linked person display info for machine hostnames (ADR-005).

Write paths store ``hostname`` on memories and queue jobs. Read paths enrich
responses with the Google-linked person's ``display_name`` and ``avatar_url``
when the machine is in ``linked`` status.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from app.utils.identity import resolve_hostname


@dataclass(frozen=True)
class CreatorIdentity:
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    # Google / person e-mail when known (prefer over hostname@mem0.local in PLANKA).
    email: Optional[str] = None


def _normalize_hostnames(hostnames: Iterable[Optional[str]]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for raw in hostnames:
        if not raw:
            continue
        key = resolve_hostname(str(raw))
        if not key or key == "unknown-host" or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def resolve_creator_identities_with_db(
    db: Session,
    hostnames: Iterable[Optional[str]],
) -> dict[str, CreatorIdentity]:
    """Batch-resolve hostnames using an existing SQLAlchemy session."""
    keys = _normalize_hostnames(hostnames)
    if not keys:
        return {}

    from app.models import Machine, MachineStatus, User

    try:
        # This lookup is optional enrichment. If a rolling deployment leaves
        # identity tables/columns temporarily out of sync, PostgreSQL marks the
        # current transaction as failed after the SELECT error. Keep that error
        # inside a savepoint so the caller can continue its primary read.
        with db.begin_nested():
            rows = (
                db.query(
                    Machine.hostname,
                    User.display_name,
                    User.avatar_url,
                    User.name,
                    User.email,
                )
                .join(User, Machine.linked_user_id == User.id)
                .filter(
                    Machine.hostname.in_(keys),
                    Machine.status == MachineStatus.linked,
                    Machine.linked_user_id.isnot(None),
                )
                .all()
            )
        return {
            hostname: CreatorIdentity(
                display_name=display_name or name,
                avatar_url=avatar_url,
                email=(email or "").strip().lower() or None,
            )
            for hostname, display_name, avatar_url, name, email in rows
        }
    except Exception:  # noqa: BLE001 - enrichment is best-effort on read paths
        return {}


def resolve_creator_identities(
    hostnames: Iterable[Optional[str]],
) -> dict[str, CreatorIdentity]:
    """Batch-resolve hostnames to linked person display fields.

    Returns a map keyed by normalized hostname. Missing or unlinked hostnames
    are omitted (best-effort; never raises).
    """
    keys = _normalize_hostnames(hostnames)
    if not keys:
        return {}

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return resolve_creator_identities_with_db(db, keys)
    except Exception:  # noqa: BLE001 - enrichment is best-effort on read paths
        return {}
    finally:
        db.close()


def identity_for_hostname(
    hostname: Optional[str],
    identities: dict[str, CreatorIdentity],
) -> Optional[CreatorIdentity]:
    if not hostname:
        return None
    return identities.get(resolve_hostname(str(hostname)))


def enrich_memory_attribution(
    item: dict[str, Any],
    identities: dict[str, CreatorIdentity],
    *,
    hostname_key: str = "created_by_hostname",
) -> None:
    """Attach ``created_by_display_name`` / ``created_by_avatar_url`` in-place."""
    identity = identity_for_hostname(item.get(hostname_key), identities)
    if identity is None:
        return
    if identity.display_name:
        item["created_by_display_name"] = identity.display_name
    if identity.avatar_url:
        item["created_by_avatar_url"] = identity.avatar_url


def enrich_memory_items(
    items: list[dict[str, Any]],
    *,
    hostname_key: str = "created_by_hostname",
) -> None:
    """Resolve and attach creator identity fields for a list of memory dicts."""
    identities = resolve_creator_identities(item.get(hostname_key) for item in items)
    for item in items:
        enrich_memory_attribution(item, identities, hostname_key=hostname_key)


def enrich_actor_fields(
    item: dict[str, Any],
    identities: dict[str, CreatorIdentity],
    *,
    hostname_key: str = "hostname",
    display_name_key: str = "display_name",
    avatar_url_key: str = "avatar_url",
) -> None:
    """Attach person display fields for queue/audit/access-log rows."""
    identity = identity_for_hostname(item.get(hostname_key), identities)
    if identity is None:
        return
    if identity.display_name:
        item[display_name_key] = identity.display_name
    if identity.avatar_url:
        item[avatar_url_key] = identity.avatar_url


def enrich_actor_items(
    items: list[dict[str, Any]],
    *,
    hostname_key: str = "hostname",
    display_name_key: str = "display_name",
    avatar_url_key: str = "avatar_url",
) -> None:
    identities = resolve_creator_identities(item.get(hostname_key) for item in items)
    for item in items:
        enrich_actor_fields(
            item,
            identities,
            hostname_key=hostname_key,
            display_name_key=display_name_key,
            avatar_url_key=avatar_url_key,
        )


def _identity_lookup_key(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    key = str(raw).strip()
    return key or None


def resolve_actor_identities_with_db(
    db: Session,
    actors: Iterable[Optional[str]],
) -> dict[str, CreatorIdentity]:
    """Resolve assignee/author keys to linked person display info.

    Accepts machine hostnames (preferred), e-mails, ``User.user_id``, or
    ``User.id`` (UUID string). Map keys include the original actor string and
    normalized hostname/e-mail variants so callers can look up either form.
    Best-effort: never raises.
    """
    from uuid import UUID

    from app.models import User

    raw_keys = []
    seen: set[str] = set()
    for raw in actors:
        key = _identity_lookup_key(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        raw_keys.append(key)
    if not raw_keys:
        return {}

    result: dict[str, CreatorIdentity] = {}

    try:
        host_map = resolve_creator_identities_with_db(db, raw_keys)
        for raw in raw_keys:
            identity = identity_for_hostname(raw, host_map)
            if identity is None:
                continue
            result[raw] = identity
            result[resolve_hostname(raw)] = identity

        remaining = [k for k in raw_keys if k not in result]
        if not remaining:
            return result

        emails: list[str] = []
        user_ids: list[str] = []
        uuids: list[UUID] = []
        for key in remaining:
            try:
                uuids.append(UUID(key))
                continue
            except ValueError:
                pass
            if "@" in key:
                emails.append(key.lower())
            else:
                user_ids.append(key)

        from sqlalchemy import func as sa_func
        from sqlalchemy import or_

        clauses = []
        if emails:
            clauses.append(sa_func.lower(User.email).in_(emails))
        if user_ids:
            clauses.append(User.user_id.in_(user_ids))
        if uuids:
            clauses.append(User.id.in_(uuids))

        users: list[User] = []
        if clauses:
            # Like hostname enrichment above, this is display-only data. A
            # schema mismatch must not poison the session used by list_tasks.
            with db.begin_nested():
                users = db.query(User).filter(or_(*clauses)).all()

        # Deduplicate by id while registering all lookup aliases.
        seen_user_ids: set[Any] = set()
        for user in users:
            if user.id in seen_user_ids:
                continue
            seen_user_ids.add(user.id)
            identity = CreatorIdentity(
                display_name=user.display_name or user.name,
                avatar_url=user.avatar_url,
                email=(user.email or "").strip().lower() or None,
            )
            result[str(user.id)] = identity
            if user.user_id:
                result[user.user_id] = identity
            if user.email:
                result[user.email] = identity
                result[user.email.lower()] = identity

        for raw in remaining:
            if raw in result:
                continue
            lowered = raw.lower()
            if lowered in result:
                result[raw] = result[lowered]
    except Exception:  # noqa: BLE001 - enrichment is best-effort on read paths
        return result

    return result


def identity_for_actor(
    actor: Optional[str],
    identities: dict[str, CreatorIdentity],
) -> Optional[CreatorIdentity]:
    key = _identity_lookup_key(actor)
    if not key:
        return None
    if key in identities:
        return identities[key]
    lowered = key.lower()
    if lowered in identities:
        return identities[lowered]
    return identity_for_hostname(key, identities)


# --------------------------------------------------------------------------- #
# Reader / actor strings as stored in ``read_audit_logs``
#
# The ``hostname`` column holds the reader, but how far it can be trusted
# depends on the row's ``source`` (channel):
#   * Web UI (``api`` / ``admin``): ``ui:<User.id>`` is written ONLY by
#     ``ui_reader_actor`` from a server-validated session JWT (``sub``), so it is
#     resolved to that person. Any other ``ui:`` value (``ui:anonymous``, the
#     shared build id ``ui:S0293`` in historical rows, …) is anonymous.
#   * Every other channel (``mcp``, ``compat_v3``, unknown): the value is
#     client-asserted (MCP path segment / compat header). It is resolved ONLY as
#     a machine hostname (linked ``Machine``), exactly as before card 01ada614.
#     ``ui:<uuid>``, a bare UUID, an e-mail or a ``User.user_id`` are treated as
#     plain hostnames and never as a session/person reference — otherwise any
#     agent could impersonate a colleague by sending their id.
# --------------------------------------------------------------------------- #
UI_ACTOR_PREFIX = "ui:"
# Neutral actor for Web UI reads without a Google session. The REST ``?user_id=``
# sent by the UI is the build-time ``NEXT_PUBLIC_USER_ID`` (``OPENMEMORY_UI_USER_ID``)
# shared by EVERY browser, so it must never be shown as a person.
UI_ANONYMOUS_ACTOR = "ui:anonymous"
UI_ANONYMOUS_LABEL = "Interface Web (sem login)"
# Sources written by the Web UI routers, where ``ui:<uuid>`` comes from a session.
WEB_SESSION_SOURCES = frozenset({"api", "admin"})


def is_web_session_source(source: Optional[str]) -> bool:
    return (source or "").strip().lower() in WEB_SESSION_SOURCES


def split_ui_actor(raw: Optional[str]) -> Optional[str]:
    """Return ``user_id`` for ``ui:<user_id>`` actors, else ``None``."""
    if not raw:
        return None
    text = str(raw).strip()
    if not text.startswith(UI_ACTOR_PREFIX):
        return None
    inner = text[len(UI_ACTOR_PREFIX):].strip()
    return inner or None


def session_user_pk(raw: Optional[str]):
    """``User.id`` (UUID) for a ``ui:<uuid>`` actor, else ``None``.

    Only meaningful for Web UI rows (:data:`WEB_SESSION_SOURCES`).
    """
    from uuid import UUID

    inner = split_ui_actor(raw)
    if not inner:
        return None
    try:
        return UUID(inner)
    except ValueError:
        return None


def is_anonymous_ui_actor(raw: Optional[str]) -> bool:
    """True for Web UI ``ui:`` actors that carry no session person.

    Only ``ui:<User.id UUID>`` identifies a person; every other ``ui:`` value is
    anonymous — including historical ``ui:<NEXT_PUBLIC_USER_ID>`` rows (e.g.
    ``ui:S0293``), written for every viewer. Independent of
    ``OPENMEMORY_UI_USER_ID`` being set on the API container.
    """
    if not raw or not str(raw).strip().startswith(UI_ACTOR_PREFIX):
        return False
    return session_user_pk(raw) is None


def _has_display(identity: Optional[CreatorIdentity]) -> bool:
    return bool(identity and (identity.display_name or identity.avatar_url))


def _resolve_session_readers(db: Session, actors: set[str]) -> dict[str, CreatorIdentity]:
    """``ui:<User.id>`` → person, by primary key only (no e-mail / user_id)."""
    from app.models import User

    pks = {actor: session_user_pk(actor) for actor in actors}
    wanted = {pk for pk in pks.values() if pk is not None}
    if not wanted:
        return {}
    users = {user.id: user for user in db.query(User).filter(User.id.in_(wanted)).all()}
    result: dict[str, CreatorIdentity] = {}
    for actor, pk in pks.items():
        user = users.get(pk)
        if user is None:
            continue
        identity = CreatorIdentity(
            display_name=user.display_name or user.name,
            avatar_url=user.avatar_url,
            email=(user.email or "").strip().lower() or None,
        )
        if _has_display(identity):
            result[actor] = identity
    return result


def _resolve_hostname_readers(db: Session, actors: set[str]) -> dict[str, CreatorIdentity]:
    """Client-asserted actors → person via linked machine hostname only."""
    host_map = resolve_creator_identities_with_db(db, actors)
    result: dict[str, CreatorIdentity] = {}
    for actor in actors:
        identity = identity_for_hostname(actor, host_map)
        if _has_display(identity):
            result[actor] = identity  # type: ignore[assignment]
    return result


def resolve_reader_identities_with_db(
    db: Session,
    readers: Iterable[tuple[Optional[str], Optional[str]]],
) -> dict[tuple[str, bool], CreatorIdentity]:
    """Resolve ``(actor, source)`` pairs from ``read_audit_logs`` to people.

    Returns a map keyed by ``(actor, is_web_session_source)``. Web UI rows
    resolve only ``ui:<User.id>``; all other channels resolve only by machine
    hostname (see module notes above). Identities without a name/avatar are
    omitted so callers keep the hostname label. Best-effort: never raises.
    """
    session_actors: set[str] = set()
    host_actors: set[str] = set()
    requested: set[tuple[str, bool]] = set()
    for actor, source in readers:
        text = str(actor or "").strip()
        if not text:
            continue
        web = is_web_session_source(source)
        if web:
            if session_user_pk(text) is not None:
                session_actors.add(text)
            elif text.startswith(UI_ACTOR_PREFIX):
                continue  # anonymous / shared build id: never a person
            else:
                host_actors.add(text)  # legacy UI rows without prefix: hostname only
        else:
            host_actors.add(text)
        requested.add((text, web))

    result: dict[tuple[str, bool], CreatorIdentity] = {}
    try:
        by_session = _resolve_session_readers(db, session_actors)
        by_host = _resolve_hostname_readers(db, host_actors)
    except Exception:  # noqa: BLE001 - enrichment is best-effort on read paths
        return result
    for text, web in requested:
        identity = by_session.get(text) if web and text in session_actors else by_host.get(text)
        if identity is not None:
            result[(text, web)] = identity
    return result


def enrich_reader_items(
    items: list[dict[str, Any]],
    *,
    actor_key: str = "hostname",
    source_key: str = "source",
    display_name_key: str = "display_name",
    avatar_url_key: str = "avatar_url",
) -> None:
    """Attach person name/avatar to access-log rows, honouring the channel.

    Web UI rows without a session person get the neutral label and no avatar.
    """
    readers: list[tuple[str, Optional[str]]] = []
    for item in items:
        actor = str(item.get(actor_key) or "").strip()
        web = is_web_session_source(item.get(source_key))
        if web and is_anonymous_ui_actor(actor):
            item[display_name_key] = UI_ANONYMOUS_LABEL
            item.pop(avatar_url_key, None)
            item["anonymous"] = True
            continue
        if actor:
            readers.append((actor, item.get(source_key)))
    if not readers:
        return

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        identities = resolve_reader_identities_with_db(db, readers)
    except Exception:  # noqa: BLE001 - enrichment is best-effort on read paths
        return
    finally:
        db.close()

    for item in items:
        if item.get("anonymous"):
            continue
        actor = str(item.get(actor_key) or "").strip()
        identity = identities.get((actor, is_web_session_source(item.get(source_key))))
        if identity is None:
            continue
        if identity.display_name:
            item[display_name_key] = identity.display_name
        if identity.avatar_url:
            item[avatar_url_key] = identity.avatar_url
