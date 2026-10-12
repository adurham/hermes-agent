"""A closed agent's message bodies leave the token-estimate memo.

``_MSG_TOKENS_CACHE`` pins every string it fingerprints, so after ``AIAgent.close()`` a long-lived
``hermes serve`` kept each closed session's message bodies resident until 4096 newer messages evicted
them (~50 MB after 20 sessions x 10 turns of 256 KB replies on macOS).
"""

import threading

from agent.model_metadata import _MSG_TOKENS_CACHE, estimate_messages_tokens_rough
from run_agent import AIAgent


def _pinned_ids() -> set[int]:
    return {id(pin) for pins, _text, _images in _MSG_TOKENS_CACHE.values() for pin in pins}


def _closing_agent(messages: list) -> AIAgent:
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = "memo-release"
    agent.client = None
    agent._active_children_lock = threading.Lock()
    agent._active_children = set()
    agent._end_session_on_close = False
    agent._session_messages = messages
    return agent


def test_close_evicts_the_conversations_memo_entries_and_keeps_others():
    reply = "closed-session reply " * 5000
    other = "another live session's reply " * 5000
    closed = [{"role": "user", "content": "hi there, a long enough opening question for the memo key?"},
              {"role": "assistant", "content": reply}]
    live = [{"role": "assistant", "content": other}]
    before = estimate_messages_tokens_rough(closed)
    estimate_messages_tokens_rough(live)
    assert id(reply) in _pinned_ids() and id(other) in _pinned_ids()

    _closing_agent(closed).close()

    assert id(reply) not in _pinned_ids()
    assert id(other) in _pinned_ids()
    # Eviction only drops a pure-function memo: the estimate is unchanged when recomputed.
    assert estimate_messages_tokens_rough(closed) == before
