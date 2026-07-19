"""Tests for the session-pin feature (Phase 2 of the 2026-05-19 plan).

Session-pin keeps a warm-tier fact visible in the system prompt for the
remainder of the current session — gone after session restart. Fills
the gap between hot tier (permanent, cap-limited) and warm tier
(searchable but invisible).

We exercise:
  * ``agent.fork.memory_session_pin`` helpers — pin/unpin/list/render
  * ``tools.memory_tool`` ``pin`` / ``unpin`` / ``pinned`` actions
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agent.fork import memory_session_pin


class FakeAgent:
    """Stub agent with only the attributes session-pin touches."""

    def __init__(
        self,
        *,
        max_count: int = 5,
        max_chars: int = 2000,
    ):
        self._session_pinned_facts: dict[int, dict] = {}
        self._session_pin_max_count = max_count
        self._session_pin_max_chars = max_chars


# ---------------------------------------------------------------------------
# init_state
# ---------------------------------------------------------------------------


class TestInitState:
    def test_sets_defaults(self):
        class Bare:
            pass

        a = Bare()
        memory_session_pin.init_state(a)
        assert a._session_pinned_facts == {}
        assert a._session_pin_max_count == 5
        assert a._session_pin_max_chars == 2000


# ---------------------------------------------------------------------------
# pin_fact
# ---------------------------------------------------------------------------


class TestPinFact:
    def test_pins_existing_fact(self, monkeypatch):
        a = FakeAgent()
        fake_row = {
            "fact_id": 69,
            "content": "READ THE SOURCE before concluding.",
            "category": "investigations",
            "trust_score": 0.5,
        }
        monkeypatch.setattr(
            memory_session_pin,
            "_fetch_warm_fact",
            lambda fid: fake_row if fid == 69 else None,
        )

        result = memory_session_pin.pin_fact(a, 69)
        assert result["success"] is True
        assert 69 in a._session_pinned_facts
        assert a._session_pinned_facts[69]["content"] == fake_row["content"]

    def test_refuses_missing_fact(self, monkeypatch):
        a = FakeAgent()
        monkeypatch.setattr(memory_session_pin, "_fetch_warm_fact", lambda fid: None)

        result = memory_session_pin.pin_fact(a, 999)
        assert result["success"] is False
        assert "999" in result["error"]
        assert a._session_pinned_facts == {}

    def test_already_pinned_is_idempotent(self, monkeypatch):
        a = FakeAgent()
        fake_row = {"fact_id": 69, "content": "X", "trust_score": 0.5}
        monkeypatch.setattr(memory_session_pin, "_fetch_warm_fact", lambda fid: fake_row)

        memory_session_pin.pin_fact(a, 69)
        result = memory_session_pin.pin_fact(a, 69)
        # Idempotent: success, but tells the caller it was already pinned.
        assert result["success"] is True
        assert "already" in result.get("message", "").lower()
        assert len(a._session_pinned_facts) == 1

    def test_evicts_oldest_when_at_max_count(self, monkeypatch):
        a = FakeAgent(max_count=2)
        # Pin three facts; the first should be evicted.
        rows = {
            i: {"fact_id": i, "content": f"fact {i}", "trust_score": 0.5}
            for i in (1, 2, 3)
        }
        monkeypatch.setattr(
            memory_session_pin, "_fetch_warm_fact", lambda fid: rows.get(fid),
        )

        r1 = memory_session_pin.pin_fact(a, 1)
        r2 = memory_session_pin.pin_fact(a, 2)
        r3 = memory_session_pin.pin_fact(a, 3)
        assert r1["success"] and r2["success"] and r3["success"]
        # Only the two most recent remain.
        assert 1 not in a._session_pinned_facts
        assert 2 in a._session_pinned_facts
        assert 3 in a._session_pinned_facts
        # The third response signals eviction.
        assert "evicted" in r3.get("message", "").lower() or 1 in r3.get("evicted", [])

    def test_refuses_oversized_pin(self, monkeypatch):
        a = FakeAgent(max_chars=100)
        big_row = {
            "fact_id": 1,
            "content": "X" * 150,
            "trust_score": 0.5,
        }
        monkeypatch.setattr(memory_session_pin, "_fetch_warm_fact", lambda fid: big_row)

        result = memory_session_pin.pin_fact(a, 1)
        assert result["success"] is False
        assert "char" in result["error"].lower() or "size" in result["error"].lower()
        assert 1 not in a._session_pinned_facts


# ---------------------------------------------------------------------------
# unpin_fact
# ---------------------------------------------------------------------------


class TestUnpinFact:
    def test_unpins_existing(self, monkeypatch):
        a = FakeAgent()
        a._session_pinned_facts[42] = {"fact_id": 42, "content": "x"}
        result = memory_session_pin.unpin_fact(a, 42)
        assert result["success"] is True
        assert 42 not in a._session_pinned_facts

    def test_unpinning_missing_is_no_op(self):
        a = FakeAgent()
        result = memory_session_pin.unpin_fact(a, 999)
        # Soft no-op: success=False with a clear message, no crash.
        assert result["success"] is False
        assert "not pinned" in result["error"].lower() or "999" in result["error"]


# ---------------------------------------------------------------------------
# list_pinned
# ---------------------------------------------------------------------------


class TestListPinned:
    def test_empty(self):
        a = FakeAgent()
        result = memory_session_pin.list_pinned(a)
        assert result["success"] is True
        assert result["count"] == 0
        assert result["pinned"] == []

    def test_lists_in_pin_order(self, monkeypatch):
        a = FakeAgent()
        # Pin in order 5, 1, 9
        rows = {
            5: {"fact_id": 5, "content": "five", "trust_score": 0.5},
            1: {"fact_id": 1, "content": "one", "trust_score": 0.5},
            9: {"fact_id": 9, "content": "nine", "trust_score": 0.5},
        }
        monkeypatch.setattr(
            memory_session_pin, "_fetch_warm_fact", lambda fid: rows.get(fid),
        )
        for i in (5, 1, 9):
            memory_session_pin.pin_fact(a, i)

        result = memory_session_pin.list_pinned(a)
        ids = [p["fact_id"] for p in result["pinned"]]
        # Python dict preserves insertion order — pin order is the list order.
        assert ids == [5, 1, 9]


# ---------------------------------------------------------------------------
# render_pinned_block
# ---------------------------------------------------------------------------


class TestRenderPinnedBlock:
    def test_returns_none_when_empty(self):
        a = FakeAgent()
        assert memory_session_pin.render_pinned_block(a) is None

    def test_returns_block_with_pinned_content(self, monkeypatch):
        a = FakeAgent()
        a._session_pinned_facts[69] = {
            "fact_id": 69,
            "content": "READ THE SOURCE before concluding.",
            "trust_score": 0.5,
        }
        block = memory_session_pin.render_pinned_block(a)
        assert block is not None
        assert "PINNED" in block.upper()
        assert "69" in block
        assert "READ THE SOURCE" in block

    def test_safely_handles_missing_attr(self):
        """When the agent wasn't built with the feature (test/subagent
        shape), the render call must return None instead of crashing."""

        class Bare:
            pass

        assert memory_session_pin.render_pinned_block(Bare()) is None


# ---------------------------------------------------------------------------
# memory_tool integration — pin / unpin / pinned actions
# ---------------------------------------------------------------------------


class TestMemoryToolPinActions:
    def test_pin_action_via_memory_tool(self, monkeypatch):
        from tools.memory_tool import memory_tool

        a = FakeAgent()
        fake_row = {
            "fact_id": 42,
            "content": "test fact",
            "trust_score": 0.5,
        }

        # Stub the warm-store fetch the session-pin module uses.
        monkeypatch.setattr(
            memory_session_pin, "_fetch_warm_fact", lambda fid: fake_row,
        )

        # ``pin`` is a warm-tier action — routed through _handle_warm_action.
        # We must also stub the get_warm_store call so it doesn't try to
        # initialize a real SQLite DB.
        fake_warm = MagicMock()
        with patch(
            "tools.memory_tool._get_warm_store_or_error",
            return_value=(fake_warm, None),
        ):
            raw = memory_tool(action="pin", fact_id=42, agent=a)
        result = json.loads(raw)
        assert result["success"] is True
        assert 42 in a._session_pinned_facts

    def test_unpin_action_via_memory_tool(self, monkeypatch):
        from tools.memory_tool import memory_tool

        a = FakeAgent()
        a._session_pinned_facts[42] = {"fact_id": 42, "content": "x"}

        fake_warm = MagicMock()
        with patch(
            "tools.memory_tool._get_warm_store_or_error",
            return_value=(fake_warm, None),
        ):
            raw = memory_tool(action="unpin", fact_id=42, agent=a)
        result = json.loads(raw)
        assert result["success"] is True
        assert 42 not in a._session_pinned_facts

    def test_pinned_action_via_memory_tool(self, monkeypatch):
        from tools.memory_tool import memory_tool

        a = FakeAgent()
        a._session_pinned_facts[7] = {
            "fact_id": 7,
            "content": "demo",
            "trust_score": 0.5,
        }

        fake_warm = MagicMock()
        with patch(
            "tools.memory_tool._get_warm_store_or_error",
            return_value=(fake_warm, None),
        ):
            raw = memory_tool(action="pinned", agent=a)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["count"] == 1
        assert result["pinned"][0]["fact_id"] == 7

    def test_pin_without_agent_returns_clean_error(self):
        """The pin family requires an agent reference. Without one,
        the tool must surface a clear error rather than crash."""
        from tools.memory_tool import memory_tool

        fake_warm = MagicMock()
        with patch(
            "tools.memory_tool._get_warm_store_or_error",
            return_value=(fake_warm, None),
        ):
            raw = memory_tool(action="pin", fact_id=1, agent=None)
        result = json.loads(raw)
        assert result["success"] is False
        assert "session" in result["error"].lower() or "agent" in result["error"].lower()

    def test_pin_without_fact_id_returns_error(self):
        from tools.memory_tool import memory_tool

        a = FakeAgent()
        fake_warm = MagicMock()
        with patch(
            "tools.memory_tool._get_warm_store_or_error",
            return_value=(fake_warm, None),
        ):
            raw = memory_tool(action="pin", agent=a)
        result = json.loads(raw)
        assert result["success"] is False
        assert "fact_id" in result["error"].lower()
