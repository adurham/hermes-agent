"""In-place compaction carries some tool results forward as pruned stubs (same ``message_uid``, short
body). When the full original is archived it stays display-visible, so the stub must not paint as a
second copy of the same tool result (#132939, #135655). When the original was rewound instead (the
verbatim carried tail), the stub is the only copy and stays visible."""
import json

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    instance = SessionDB(tmp_path / "state.db")
    yield instance
    instance.close()


def _seed(db, sid):
    db.create_session(sid, source="test")
    db.append_messages_batch(sid, [
        {"role": "user", "content": "read it", "timestamp": 100.0},
        {"role": "assistant", "content": "", "timestamp": 101.0,
         "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "content": "FULL " * 1000, "tool_call_id": "call-1", "tool_name": "read_file",
         "timestamp": 102.0},
        {"role": "assistant", "content": "done reading", "timestamp": 103.0},
    ])
    history = db.get_messages_as_conversation(sid, include_row_ids=True)
    history[2]["content"] = "[read_file] pruned stub"
    return history


def _painted_tool_bodies(db, sid):
    _model, display = db.get_resume_conversations(sid)
    paged = db.get_messages(sid, include_compacted=True)
    return ([m.get("content") for m in display if m["role"] == "tool"],
            [m.get("content") for m in paged if m["role"] == "tool"])


def test_pruned_copy_of_an_archived_tool_result_paints_once(db):
    sid = "archived-original"
    history = _seed(db, sid)
    db.archive_and_compact(sid, [{"role": "user", "content": "summary", "timestamp": 150.0}, *history])
    for bodies in _painted_tool_bodies(db, sid):
        assert bodies == ["FULL " * 1000]
    model_tool = [m for m in db.get_messages_as_conversation(sid) if m["role"] == "tool"]
    assert [m["content"] for m in model_tool] == ["[read_file] pruned stub"]


def test_pruned_copy_of_a_rewound_tail_original_stays_visible(db):
    sid = "rewound-original"
    history = _seed(db, sid)
    db.archive_and_compact(sid, history, tail_count=len(history))
    for bodies in _painted_tool_bodies(db, sid):
        assert bodies == ["[read_file] pruned stub"], json.dumps(bodies)[:200]
