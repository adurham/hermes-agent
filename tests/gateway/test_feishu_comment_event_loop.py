"""handle_drive_comment_event keeps the event loop free and its background reaction task owned."""

import asyncio
import gc
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.platforms.feishu import feishu_comment as fc
from plugins.platforms.feishu import feishu_comment_rules as rules


def _event(reply_id=""):
    return SimpleNamespace(event={
        "comment_id": "c1", "reply_id": reply_id, "is_mentioned": True, "timestamp": "1",
        "notice_meta": {"file_token": "docx_t", "file_type": "docx", "notice_type": "add_reply",
                        "from_user_id": {"open_id": "ou_user"}, "to_user_id": {"open_id": "ou_bot"}},
    })


def _run(event, load_config, update_comment_reaction):
    with patch.object(rules, "load_config", load_config), \
         patch.object(rules, "resolve_rule", return_value=rules.ResolvedCommentRule(True, "allowlist", frozenset(), "top")), \
         patch.object(rules, "has_wiki_keys", return_value=False), \
         patch.object(rules, "is_user_allowed", return_value=True), \
         patch.object(fc, "update_comment_reaction", update_comment_reaction), \
         patch.object(fc, "query_document_meta", AsyncMock(return_value={"title": "T", "url": "u"})), \
         patch.object(fc, "batch_query_comment", AsyncMock(return_value={"is_whole": False, "quote": ""})), \
         patch.object(fc, "_local_comment_prompt", AsyncMock(return_value="prompt")), \
         patch.object(fc, "_run_comment_agent", lambda prompt, client, session_key="": "NO_REPLY"):
        asyncio.run(fc.handle_drive_comment_event(object(), event, self_open_id="ou_bot"))


def test_comment_rules_are_loaded_off_the_event_loop_thread():
    loop_thread = threading.get_ident()
    seen = {}

    def _load_config():
        seen["thread"] = threading.get_ident()
        return object()

    _run(_event(), _load_config, AsyncMock(return_value=True))
    assert seen["thread"] != loop_thread


def test_failed_reaction_add_is_logged_with_its_traceback(caplog):
    async def _reaction(client, action, **kwargs):
        if action == "add":
            raise RuntimeError("reaction boom")
        return True

    with caplog.at_level(logging.WARNING, logger=fc.logger.name):
        _run(_event(reply_id="r1"), lambda: object(), _reaction)
        gc.collect()
    ours = [r for r in caplog.records if r.name == fc.logger.name and r.exc_info]
    assert [str(r.exc_info[1]) for r in ours] == ["reaction boom"]
    assert not fc._reaction_tasks
