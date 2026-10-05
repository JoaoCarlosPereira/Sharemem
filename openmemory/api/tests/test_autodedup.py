"""Tests for automatic near-duplicate superseding after a write.

The store accumulates paraphrases of the same fact because extraction is ADD-only
and the LLM only sees neighbours of the whole submission, never of each extracted
fact. These tests pin the safety properties of the fix, which matter more than the
detection itself: it is OFF unless asked for, it can run in report mode without
touching data, and it never fails the write that triggered it.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest

from app.utils import autodedup
from app.utils.autodedup import autodedup_after_write, find_near_duplicates


def _hit(mem_id, score, data="texto", state="active"):
    return SimpleNamespace(
        id=mem_id, score=score, payload={"data": data, "state": state}
    )


def _client(hits):
    client = MagicMock()
    client.embedding_model.embed.return_value = [0.1, 0.2, 0.3]
    client.vector_store.search.return_value = hits
    return client


def _result(*pairs):
    return {"results": [{"id": i, "memory": m, "event": "ADD"} for i, m in pairs]}


class TestMode:
    def test_off_by_default(self, monkeypatch):
        """Changing stored data must be opt-in."""
        monkeypatch.delenv("MEM0_AUTODEDUP_MODE", raising=False)
        client = _client([_hit("dup", 0.99)])

        out = autodedup_after_write(client, _result(("new", "um fato")))

        assert out["mode"] == "off"
        assert out["candidates"] == []
        client.vector_store.search.assert_not_called()

    def test_report_mode_detects_without_touching_data(self, monkeypatch):
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "report")
        client = _client([_hit("dup", 0.99)])

        with patch("app.utils.supersedes.mark_points_obsolete") as mark:
            out = autodedup_after_write(client, _result(("new", "um fato")))

        assert [c["duplicate_id"] for c in out["candidates"]] == ["dup"]
        assert "superseded" not in out
        mark.assert_not_called()

    def test_apply_mode_supersedes(self, monkeypatch):
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "apply")
        client = _client([_hit("dup", 0.99)])

        with patch(
            "app.utils.supersedes.mark_points_obsolete",
            return_value={"updated": ["dup"], "missing": []},
        ) as mark:
            out = autodedup_after_write(client, _result(("new", "um fato")))

        assert out["superseded"] == ["dup"]
        assert mark.call_args.kwargs["superseded_by"] == "new"

    def test_unknown_mode_falls_back_to_off(self, monkeypatch):
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "seila")
        assert autodedup.autodedup_mode() == "off"


class TestDetection:
    def test_below_threshold_is_not_a_duplicate(self, monkeypatch):
        monkeypatch.setenv("MEM0_AUTODEDUP_THRESHOLD", "0.95")
        client = _client([_hit("parecida", 0.90)])

        assert find_near_duplicates(client, [{"id": "new", "memory": "x"}]) == []

    def test_ignores_memories_written_by_the_same_job(self, monkeypatch):
        """A submission that states a fact twice must not supersede itself."""
        monkeypatch.setenv("MEM0_AUTODEDUP_THRESHOLD", "0.90")
        client = _client([_hit("irmao", 0.99), _hit("antiga", 0.98)])

        found = find_near_duplicates(
            client,
            [{"id": "irmao", "memory": "a"}, {"id": "novo", "memory": "b"}],
        )

        assert {c["duplicate_id"] for c in found} == {"antiga"}

    def test_ignores_already_obsolete_points(self, monkeypatch):
        monkeypatch.setenv("MEM0_AUTODEDUP_THRESHOLD", "0.90")
        client = _client([_hit("velha", 0.99, state="obsolete")])

        assert find_near_duplicates(client, [{"id": "new", "memory": "x"}]) == []

    def test_lookup_failure_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("MEM0_AUTODEDUP_THRESHOLD", "0.90")
        client = _client([])
        client.vector_store.search.side_effect = RuntimeError("qdrant fora")

        assert find_near_duplicates(client, [{"id": "new", "memory": "x"}]) == []

    @pytest.mark.parametrize("raw", ["nan", "inf", "-0.1", "1.5", "abc"])
    def test_invalid_threshold_falls_back_and_apply_still_works(self, monkeypatch, raw):
        """``nan`` made every ``score >= nan`` False — apply silently did nothing."""
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "apply")
        monkeypatch.setenv("MEM0_AUTODEDUP_THRESHOLD", raw)
        assert autodedup.autodedup_threshold() == 0.95
        client = _client([_hit("dup", 0.99), _hit("abaixo", 0.94)])

        with patch(
            "app.utils.supersedes.mark_points_obsolete",
            return_value={"updated": ["dup"], "missing": []},
        ) as mark:
            out = autodedup_after_write(client, _result(("new", "um fato")))

        assert mark.call_args.args[1] == ["dup"]
        assert out["superseded"] == ["dup"]

    @pytest.mark.parametrize("raw,expected", [("0", 0.0), ("1", 1.0), (" 0.97 ", 0.97)])
    def test_valid_threshold_unchanged(self, monkeypatch, raw, expected):
        monkeypatch.setenv("MEM0_AUTODEDUP_THRESHOLD", raw)
        assert autodedup.autodedup_threshold() == expected

    @pytest.mark.parametrize(
        "raw,threshold,expected",
        [("nan", 0.95, 0.85), ("-1", 0.95, 0.85), ("2", 0.95, 0.85), ("0.99", 0.95, 0.95),
         ("0", 0.95, 0.0), ("0.9", 0.95, 0.9), ("0.9", 0.0, 0.0)],
    )
    def test_report_floor_clamped(self, monkeypatch, raw, threshold, expected):
        monkeypatch.setenv("MEM0_AUTODEDUP_REPORT_FLOOR", raw)
        assert autodedup.autodedup_report_floor(threshold) == expected

    def test_deleted_events_are_not_candidates(self, monkeypatch):
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "report")
        client = _client([_hit("dup", 0.99)])

        out = autodedup_after_write(
            client, {"results": [{"id": "x", "memory": "m", "event": "DELETE"}]}
        )

        assert out["candidates"] == []
        client.vector_store.search.assert_not_called()


class TestReportNeverTouchesRealDatabase:
    """O modo report grava numa sessão própria; na suíte isso vai para o sandbox
    do ``tests/conftest.py`` (autouse), nunca para DATABASE_URL/./openmemory.db."""

    def test_report_mode_writes_to_sandbox(self, monkeypatch, autodedup_report_sandbox):
        monkeypatch.setenv("MEM0_AUTODEDUP_MODE", "report")
        out = autodedup_after_write(_client([_hit("dup", 0.99)]), _result(("new", "um fato")))

        assert out["recorded"] == 1
        assert autodedup_report_sandbox.count() == 1

    def test_guard_flags_dml_on_report_table(self, autodedup_report_sandbox):
        """A guarda registrada no engine real reconhece DML em autodedup_reports."""
        guard = autodedup_report_sandbox.guard
        guard(None, None, "SELECT * FROM autodedup_reports", {}, None, False)
        guard(None, None, "INSERT INTO write_queue (id) VALUES (1)", {}, None, False)
        assert autodedup_report_sandbox.violations == []
        guard(None, None, "INSERT INTO autodedup_reports (id) VALUES (?)", {}, None, False)
        assert len(autodedup_report_sandbox.violations) == 1
        # CTE: o primeiro verbo é WITH, mas o comando é DML.
        guard(
            None, None,
            "WITH old AS (SELECT id FROM autodedup_reports) DELETE FROM autodedup_reports "
            "WHERE id IN (SELECT id FROM old)",
            {}, None, False,
        )
        guard(None, None, "WITH x AS (SELECT 1) SELECT * FROM autodedup_reports", {}, None, False)
        assert len(autodedup_report_sandbox.violations) == 2
        autodedup_report_sandbox.violations.clear()  # não falhar este teste

    def test_guard_is_attached_to_real_engine(self, autodedup_report_sandbox):
        import app.database as database
        from sqlalchemy import event

        assert event.contains(database.engine, "before_cursor_execute", autodedup_report_sandbox.guard)
