"""Tests for get_cute_tool_message todo progress display.

Verifies the completion status rendering (done/total ✓) on all three
todo tool call paths: read, create (merge=False), update (merge=True).
Also verifies the checklist body rendered below the header line when the
tool result carries the full item list (see the CLI-only-shows-a-count
gap fixed in agent/display.py's "todo" branch).
"""

import json
from agent.display import get_cute_tool_message, set_tool_preview_max_len


def _todo_result(total: int, completed: int) -> str:
    """Build a fake todo_tool return value."""
    return json.dumps({
        "todos": [],
        "summary": {
            "total": total,
            "pending": total - completed,
            "in_progress": 0,
            "completed": completed,
            "cancelled": 0,
        },
    })


def _todo_result_with_items(items: list) -> str:
    """Build a fake todo_tool return value carrying full items + summary."""
    total = len(items)
    completed = sum(1 for i in items if i["status"] == "completed")
    in_progress = sum(1 for i in items if i["status"] == "in_progress")
    cancelled = sum(1 for i in items if i["status"] == "cancelled")
    pending = total - completed - in_progress - cancelled
    return json.dumps({
        "todos": items,
        "summary": {
            "total": total,
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
    })


class TestTodoRead:
    """get_cute_tool_message(…, result=…) when todos_arg is None (read path)."""

    def test_read_no_result(self):
        msg = get_cute_tool_message("todo", {}, 0.5)
        assert "reading tasks" in msg
        assert "0.5s" in msg



    def test_read_zero_total(self):
        """Edge case: empty todo list returns summary with total=0."""
        msg = get_cute_tool_message("todo", {}, 0.5,
                                    result=_todo_result(0, 0))
        assert "reading tasks" in msg




class TestTodoCreate:
    """get_cute_tool_message when merge=False (new plan creation)."""

    def test_create_default(self):
        """Brand-new plan: all pending, no result — plain count."""
        msg = get_cute_tool_message("todo",
                                    {"todos": [
                                        {"id": "a", "content": "x", "status": "pending"},
                                    ]}, 0.3)
        assert "1 task(s)" in msg
        assert "0.3s" in msg
        assert "/" not in msg  # no progress fraction



    def test_create_with_result_zero_done(self):
        """New plan with 0 done — plain count, no progress fraction."""
        msg = get_cute_tool_message("todo",
                                    {"todos": [
                                        {"id": "a", "content": "x", "status": "pending"},
                                        {"id": "b", "content": "y", "status": "pending"},
                                    ]},
                                    0.3,
                                    result=_todo_result(2, 0))
        assert "2 task(s)" in msg
        assert "/" not in msg


class TestTodoUpdate:
    """get_cute_tool_message when merge=True (incremental update)."""

    def test_update_no_result(self):
        """No result available — plain update N task(s)."""
        msg = get_cute_tool_message("todo",
                                    {"todos": [{"id": "a", "status": "completed"}],
                                     "merge": True}, 0.5)
        assert "update 1 task(s)" in msg


    def test_update_halfway(self):
        """2/4 — midpoint progress."""
        msg = get_cute_tool_message("todo",
                                    {"todos": [{"id": "b", "status": "in_progress"}],
                                     "merge": True},
                                    0.7,
                                    result=_todo_result(4, 2))
        assert "2/4" in msg
        assert "✓" in msg





    def test_update_total_not_in_summary(self):
        """Result summary missing total key."""
        msg = get_cute_tool_message("todo",
                                    {"todos": [{"id": "a", "status": "completed"}],
                                     "merge": True},
                                    0.3,
                                    result=json.dumps({"summary": {"completed": 2}}))
        assert "update 1 task(s)" in msg
        assert "✓" not in msg



class TestTodoEdgeCases:
    """Boundary cases that should not crash."""

    def test_merge_default_value(self):
        """merge defaults to False in function signature, should be False when absent."""
        msg = get_cute_tool_message("todo",
                                    {"todos": [{"id": "a", "content": "x", "status": "pending"}]},
                                    1.0)
        assert "1 task(s)" in msg


    def test_large_task_count(self):
        """Many tasks should not break formatting."""
        many = [{"id": str(i), "content": "x", "status": "pending"} for i in range(50)]
        msg = get_cute_tool_message("todo", {"todos": many}, 0.5)
        assert "50 task(s)" in msg



class TestTodoSkinIntegration:
    """Verify the skin prefix is applied to todo messages too.
    This uses the same pattern as test_skin_engine test_tool_message_uses_skin_prefix.
    """

    def test_default_skin_prefix(self):
        msg = get_cute_tool_message("todo", {}, 0.5)
        assert msg.startswith("┊")


class TestTodoChecklistBody:
    """The CLI todo line previously showed only a bare count ('7 task(s)')
    with no way to see what the tasks actually were. When the tool result
    carries the full items list (which todo_tool() always returns), the
    checklist body should render below the header line.
    """

    def _items(self):
        return [
            {"id": "1", "content": "Fix TB link", "status": "completed"},
            {"id": "2", "content": "Clear stall dumps", "status": "completed"},
            {"id": "3", "content": "Reboot both Studios", "status": "in_progress"},
            {"id": "4", "content": "Verify link back up", "status": "pending"},
            {"id": "5", "content": "Relaunch cluster", "status": "cancelled"},
        ]

    def test_checklist_renders_all_items_on_read(self):
        msg = get_cute_tool_message("todo", {}, 0.0,
                                    result=_todo_result_with_items(self._items()))
        lines = msg.splitlines()
        assert lines[0].startswith("┊ 📋 plan")
        assert "2/5 task(s)" in lines[0]
        assert len(lines) == 6  # header + 5 items
        assert "[x] Fix TB link" in lines[1]
        assert "[x] Clear stall dumps" in lines[2]
        assert "[>] Reboot both Studios" in lines[3]
        assert "[ ] Verify link back up" in lines[4]
        assert "[~] Relaunch cluster" in lines[5]

    def test_checklist_renders_on_create(self):
        items = self._items()
        msg = get_cute_tool_message("todo", {"todos": items}, 0.1,
                                    result=_todo_result_with_items(items))
        lines = msg.splitlines()
        assert len(lines) == 6
        assert "[x] Fix TB link" in lines[1]

    def test_checklist_renders_on_merge_update(self):
        items = self._items()
        msg = get_cute_tool_message(
            "todo", {"todos": [{"id": "3", "status": "in_progress"}], "merge": True},
            0.2, result=_todo_result_with_items(items))
        lines = msg.splitlines()
        assert lines[0].startswith("┊ 📋 plan      update")
        assert len(lines) == 6

    def test_no_checklist_body_when_result_has_no_items(self):
        """Backward-compat: old-shape results (no 'todos' key, or empty
        list) fall back to the header-only line -- no regression for
        callers/tests that don't pass item data."""
        msg = get_cute_tool_message("todo", {}, 0.5, result=_todo_result(4, 2))
        assert "\n" not in msg
        assert "2/4" in msg

    def test_no_checklist_body_with_no_result_at_all(self):
        msg = get_cute_tool_message("todo", {}, 0.5)
        assert "\n" not in msg

    def test_checklist_truncates_long_content(self):
        """Per-item truncation follows the same global _tool_preview_max_len
        config as every other line in this function (see _trunc's use of
        the global rather than its own `n` param)."""
        set_tool_preview_max_len(50)
        try:
            long_content = "x" * 200
            items = [{"id": "1", "content": long_content, "status": "pending"}]
            msg = get_cute_tool_message("todo", {}, 0.0,
                                        result=_todo_result_with_items(items))
            lines = msg.splitlines()
            assert len(lines) == 2
            assert len(lines[1]) < len(long_content)
        finally:
            set_tool_preview_max_len(0)

    def test_checklist_caps_at_30_with_more_marker(self):
        items = [{"id": str(i), "content": f"task {i}", "status": "pending"}
                 for i in range(40)]
        msg = get_cute_tool_message("todo", {}, 0.0,
                                    result=_todo_result_with_items(items))
        lines = msg.splitlines()
        # header + 30 shown items + 1 "more" marker line
        assert len(lines) == 32
        assert "+10 more" in lines[-1]

    def test_non_dict_items_skipped_gracefully(self):
        """Malformed items shouldn't crash rendering."""
        items = [{"id": "1", "content": "ok", "status": "pending"}, "garbage", None]
        raw_result = json.dumps({
            "todos": items,
            "summary": {"total": 1, "pending": 1,
                        "in_progress": 0, "completed": 0, "cancelled": 0},
        })
        msg = get_cute_tool_message("todo", {}, 0.0, result=raw_result)
        lines = msg.splitlines()
        assert "ok" in msg
        assert len(lines) == 2  # header + the one valid dict item


class TestWebExtractDisplay:
    """get_cute_tool_message for web_extract handles dict objects from web_search results.

    Reproduces and verifies fix for #61693 where web_search result dicts
    caused AttributeError when web_extract tried to extract domain names.
    """


    def test_web_extract_with_dict_href_field(self):
        """Dict with 'href' field (alternate key)."""
        args = {
            "urls": [
                {"href": "http://test.org/page", "title": "Test", "snippet": "..."}
            ]
        }
        msg = get_cute_tool_message("web_extract", args, 0.3)
        assert "test.org" in msg




    def test_web_extract_with_mixed_types(self):
        """Mix of string URLs and dict objects."""
        args = {
            "urls": [
                "https://direct.com/page",
                {"url": "https://dict.com/page", "title": "Dict URL"},
            ]
        }
        msg = get_cute_tool_message("web_extract", args, 0.4)
        # First item is a string, so domain should come from it
        assert "direct.com" in msg

    def test_web_extract_empty_urls(self):
        """Empty urls list - shows 'pages' placeholder."""
        args = {"urls": []}
        msg = get_cute_tool_message("web_extract", args, 0.1)
        assert "pages" in msg
        assert "📄" in msg
