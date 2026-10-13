"""The AIAgent surface main's HermesCLI uses, backed by the shared gateway session (CALLMAP §3.2-3.3).

``run_conversation`` submits ``prompt.submit`` and replays the gateway's event stream, on the
calling (agent) thread, into the SAME callbacks HermesCLI passed to ``AIAgent`` on main, then
returns a dict shaped like ``AIAgent.run_conversation``'s. The CLI process never builds an agent,
never runs a tool and never writes ``state.db``: the turn runs in the gateway.

Event contract: wave_a.md (frozen). Every frame is a gateway ``event`` with params
``{type, session_id, execution_generation, admission_id?, payload}``.
"""
from __future__ import annotations

import logging
import queue
import time
import uuid

from hermes_cli.gateway_client import GatewayClientError
from hermes_cli.gateway_connection import DISCONNECTED

logger = logging.getLogger(__name__)

# How long a turn waits for its settlement after an interrupt (main: cli_chat_turn_mixin 10 s).
INTERRUPT_SETTLE_SECONDS = 10.0
_POLL = 0.1

_RESULT_DEFAULTS = {
    "final_response": "", "failed": False, "partial": False, "error": None, "failure_reason": None,
    "completed": True, "interrupted": False, "interrupt_message": None, "api_calls": 0,
    "pending_steer": None, "last_reasoning": None, "response_previewed": False,
    "response_transformed": False, "pre_transform_response": None, "billing_block": None,
    "auxiliary_usage": {},
}
# Usage counters the status bar / /usage read straight off the agent (cli_status_bar_mixin).
_COUNTER_FROM_USAGE = {
    "session_prompt_tokens": "input_tokens", "session_input_tokens": "input_tokens",
    "session_completion_tokens": "output_tokens", "session_output_tokens": "output_tokens",
    "session_cache_read_tokens": "cache_read_tokens", "session_cache_write_tokens": "cache_write_tokens",
    "session_reasoning_tokens": "reasoning_tokens",
}


class _CompressorView:
    """``agent.context_compressor`` as the status bar / banner read it (plain attributes)."""

    def __init__(self):
        self.last_prompt_tokens = 0
        self.last_real_prompt_tokens = None
        self.context_length = 0
        self.compression_count = 0
        self._config_context_length = None
        self.threshold_tokens = 0


class _TodoView:
    def __init__(self):
        self.items: list = []

    def read(self):
        return list(self.items)


class GatewayAgent:
    """Gateway-backed stand-in for ``run_agent.AIAgent`` (constructed at the same call site)."""

    platform = "cli"
    _supports_active_turn_redirect = True

    def __init__(self, *, session_db=None, session_id=None, model=None, provider=None, base_url=None,
                 api_mode=None, max_iterations=90, quiet_mode=True, tool_progress_mode="all",
                 reasoning_config=None, service_tier=None, clarify_callback=None, connection_callback=None,
                 reasoning_callback=None, thinking_callback=None, tool_progress_callback=None,
                 tool_start_callback=None, tool_complete_callback=None, stream_delta_callback=None,
                 tool_gen_callback=None, notice_callback=None, notice_clear_callback=None,
                 reaction_callback=None, interim_assistant_callback=None, connection=None, **_ignored):
        conn = connection or getattr(session_db, "connection", None)
        if conn is None:
            raise GatewayClientError("Gateway session unavailable; no local fallback")
        self.connection = conn
        self._session_db = session_db
        self.session_id = session_id
        self.model = model or ""
        self.provider = provider or ""
        self.base_url = base_url or ""
        self.api_mode = api_mode
        self.api_key = None  # never on this side of the wire
        self.max_iterations = max_iterations
        self.quiet_mode = quiet_mode
        self.tool_progress_mode = tool_progress_mode
        self.reasoning_config = reasoning_config
        self.service_tier = service_tier
        self.clarify_callback = clarify_callback
        self.connection_callback = connection_callback
        self.reasoning_callback = reasoning_callback
        self.thinking_callback = thinking_callback
        self.tool_progress_callback = tool_progress_callback
        self.tool_start_callback = tool_start_callback
        self.tool_complete_callback = tool_complete_callback
        self.stream_delta_callback = stream_delta_callback
        self.tool_gen_callback = tool_gen_callback
        self.notice_callback = notice_callback
        self.notice_clear_callback = notice_clear_callback
        self.reaction_callback = reaction_callback
        self.interim_assistant_callback = interim_assistant_callback
        self.suppress_status_output = False
        self._print_fn = None
        # Inert private attributes main's CLI writes (CALLMAP §3.2).
        self._last_flushed_db_idx = 0
        self._session_messages: list = []
        self._db_flush_scan_prefix = None
        self._persist_user_message_idx = self._persist_user_message_override = None
        self._persist_user_message_timestamp = None
        self._pending_cli_user_message = None
        self._voice_turn_pending = False
        self._auto_load_skills_result = None
        self._auto_load_skills_resolved = False
        self._session_db_created = True
        self._fallback_activated = False
        self._rate_limited_until = None
        self._primary_runtime = None
        self._interrupt_requested = False
        self._active_children: list = []
        self._api_latency_history: list = []
        self._api_output_history: list = []
        self._turn_base_usage_anchor = None
        self._current_task_id = self._current_turn_id = self._current_api_request_id = ""
        self._memory_manager = None
        self._memory_store = None
        self._checkpoint_mgr = None
        self.session_api_calls = 0
        self.session_total_tokens = 0
        self.session_prompt_tokens = self.session_input_tokens = 0
        self.session_completion_tokens = self.session_output_tokens = 0
        self.session_cache_read_tokens = self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.context_compressor = _CompressorView()
        self._todo_store = _TodoView()
        self._generation = None
        self._admission = None
        self._interrupt_sent_at = None
        self._apply_info(self._state().info)

    # -- snapshot-backed reads ---------------------------------------------------------------
    def _state(self):
        return self.connection.state(self.session_id)

    def _apply_info(self, info):
        info = info or {}
        if info.get("model"):
            self.model = info["model"]
        if info.get("provider"):
            self.provider = info["provider"]
        if info.get("context_length"):
            self.context_compressor.context_length = int(info["context_length"])

    @property
    def tools(self):
        """Tool schemas the gateway session will load (``info.tools`` {toolset: [names]})."""
        groups = (self._state().info or {}).get("tools") or {}
        names = [name for members in groups.values() for name in (members or [])]
        return [{"type": "function", "function": {"name": name}} for name in names]

    @property
    def valid_tool_names(self):
        return {tool["function"]["name"] for tool in self.tools}

    def get_rate_limit_state(self):
        return None

    # -- no-ops: the gateway owns persistence, prompt cache, memory and clients --------------
    def _flush_messages_to_session_db(self, *a, **k):
        return None

    def _persist_session(self, *a, **k):
        return None

    def _ensure_db_session(self, *a, **k):
        return None

    def _invalidate_system_prompt(self, *a, **k):
        return None

    def reset_session_state(self, *a, **k):
        return None

    def release_clients(self, *a, **k):
        return None

    def clear_interrupt(self, *a, **k):
        self._interrupt_requested = False

    def shutdown_memory_provider(self, *a, **k):
        return None

    def close(self, *a, **k):
        return None

    def _restore_primary_runtime(self, *a, **k):
        return None

    def _summarize_api_error(self, exc):
        return str(exc)[:300]

    # -- controls ------------------------------------------------------------------------------
    def interrupt(self, message=None):
        """``session.interrupt`` for the running generation; ``message`` stays CLI-side (main
        re-queues it as the next turn itself)."""
        self._interrupt_requested = True
        if self._interrupt_sent_at is None:
            self._interrupt_sent_at = time.monotonic()
        generation = self._generation
        if generation is None:
            generation = self._state().execution_generation
        try:
            self.connection.call("session.interrupt", session_id=self.session_id,
                                 execution_generation=generation, timeout=5)
        except Exception as exc:  # the settle wait below bounds a lost interrupt
            logger.debug("session.interrupt failed: %s", exc)

    def hard_interrupt(self, message=None, tool_reason=None):
        self.interrupt(message)

    def _correct(self, verb, text):
        generation = self._generation
        if generation is None:
            return False
        try:
            result = self.connection.call(f"session.{verb}", session_id=self.session_id, text=text,
                                          execution_generation=generation)
        except GatewayClientError:
            return False
        return (result or {}).get("status") in {"queued", "redirected"}

    def _compress_context(self, messages, system_message=None, *, approx_tokens=None, focus_topic=None,
                          force=True, verbatim_tail=None, **_kw):
        """Main's manual ``/compress`` seam (``compress_now``) as ``session.mutate compress``: the
        gateway summarizes and commits; the returned transcript is the gateway's compacted history.
        ``verbatim_tail`` rows are stamped as stored, so ``compress_now`` does not rejoin them."""
        from agent.context_compressor import _DB_PERSISTED_MARKER
        from agent.conversation_compression import COMPACTION_STATUS
        from hermes_cli.gateway_client import COMPRESS_RPC_TIMEOUT
        payload = {}
        if focus_topic:
            payload["focus"] = focus_topic
        if verbatim_tail:
            payload["partial"] = True
            payload["keep_last"] = max(1, sum(1 for m in verbatim_tail if m.get("role") == "user"))
        self._emit_print(COMPACTION_STATUS)
        snapshot = self.connection.call("session.resume", session_id=self.session_id)
        self.connection.call("session.mutate", timeout=COMPRESS_RPC_TIMEOUT, session_id=self.session_id,
                             request_id=uuid.uuid4().hex, expected_revision=snapshot["revision"],
                             expected_generation=snapshot["execution_generation"],
                             operation="compress", payload=payload)
        for row in verbatim_tail or ():
            row[_DB_PERSISTED_MARKER] = True
        after = self.connection.call("session.resume", session_id=self.session_id)
        self._state().adopt_snapshot(after)
        compressor = self.context_compressor
        compressor.compression_count += 1
        compressor.last_prompt_tokens = -1
        self._turn_base_usage_anchor = None
        return [{k: v for k, v in m.items() if k != "row_id"} for m in after.get("messages") or []], system_message

    def steer(self, text):
        return self._correct("steer", text)

    def redirect(self, text):
        return self._correct("redirect", text)

    # -- the turn ------------------------------------------------------------------------------
    def _submit_params(self, user_message, persist_user_message, turn_author):
        text = persist_user_message if isinstance(persist_user_message, str) else user_message
        params = {"session_id": self.session_id, "submission_id": uuid.uuid4().hex, "text": text}
        if isinstance(user_message, str) and user_message != text:
            params["api_text"] = user_message
        if turn_author:
            params["turn_author"] = turn_author
        if self._voice_turn_pending:
            params["voice_turn"] = True
        staged = self._pending_cli_user_message
        if isinstance(staged, dict) and staged.get("display_kind"):
            params["display_kind"] = staged["display_kind"]
        if getattr(self, "_finite", False):
            params["finite"] = True
        return params

    def run_conversation(self, user_message, conversation_history=None, stream_callback=None,
                         task_id=None, persist_user_message=None, turn_author=None, **_kw):
        history = list(conversation_history or [])
        self._interrupt_requested = False
        self._interrupt_sent_at = None
        self._drain_stale()
        try:
            receipt = self.connection.call("prompt.submit", **self._submit_params(
                user_message, persist_user_message, turn_author))
        except GatewayClientError as exc:
            return self._failed(history, user_message, str(exc))
        self._admission = (receipt or {}).get("admission_id")
        if receipt and receipt.get("execution_generation") is not None:
            self._generation = receipt["execution_generation"]
        starts: dict = {}
        try:
            outcome = self._pump(stream_callback, starts)
        finally:
            self._generation_done()
        if isinstance(outcome, Exception):
            return self._failed(history, user_message, str(outcome) or DISCONNECTED)
        return self._result(history, user_message, outcome)

    def _drain_stale(self):
        """Frames left from an earlier turn of this viewer (a late settlement) are not this turn's."""
        while True:
            try:
                frame = self.connection.events.get_nowait()
            except queue.Empty:
                return
            if isinstance(frame, Exception):
                self.connection.events.put(frame)
                return

    def _generation_done(self):
        self._generation = None

    def _pump(self, stream_callback, starts):
        """Dispatch frames until this admission's ``message.complete``; returns its payload, or the
        transport error (disconnect)."""
        events = self.connection.events
        while True:
            if (self._interrupt_sent_at is not None
                    and time.monotonic() - self._interrupt_sent_at > INTERRUPT_SETTLE_SECONDS):
                return {"status": "interrupted", "outcome": "cancelled", "_settle_timeout": True}
            try:
                frame = events.get(timeout=_POLL)
            except queue.Empty:
                continue
            if isinstance(frame, Exception):
                return frame
            params = frame.get("params") if isinstance(frame, dict) else None
            if not isinstance(params, dict) or params.get("session_id") != self.session_id:
                continue
            kind, payload = params.get("type"), params.get("payload") or {}
            admission = params.get("admission_id") or (payload.get("admission_id") if isinstance(payload, dict) else None)
            if self._admission and admission and admission != self._admission:
                continue
            if params.get("execution_generation") is not None:
                self._generation = params["execution_generation"]
                self._state().execution_generation = params["execution_generation"]
            if kind == "message.complete":
                return payload
            if kind == "session.replay_gap":
                return GatewayClientError("session_replay_gap: " + DISCONNECTED)
            try:
                self._dispatch(kind, payload, stream_callback, starts)
            except Exception:
                logger.debug("event %s dispatch failed", kind, exc_info=True)

    def _dispatch(self, kind, payload, stream_callback, starts):
        handler = _HANDLERS.get(kind)
        if handler is not None:
            handler(self, payload, stream_callback, starts)

    # event handlers (agent thread) ------------------------------------------------------------
    def _on_delta(self, payload, stream_callback, starts):
        text = payload.get("text")
        if self.stream_delta_callback is not None:
            self.stream_delta_callback(text)
        if stream_callback is not None and isinstance(text, str):
            stream_callback(text)

    def _on_interim(self, payload, stream_callback, starts):
        text = payload.get("text")
        if self.interim_assistant_callback is not None:
            self.interim_assistant_callback(text, already_streamed=bool(payload.get("already_streamed")))
        elif text and str(text).strip():
            self._emit_print(text)

    def _on_tool_generating(self, payload, stream_callback, starts):
        if self.tool_gen_callback is not None:
            self.tool_gen_callback(payload.get("name"))

    def _on_tool_start(self, payload, stream_callback, starts):
        name = payload.get("name") or payload.get("tool_name") or "tool"
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        call_id = payload.get("tool_id") or payload.get("tool_call_id") or ""
        starts[call_id] = time.monotonic()
        if self.tool_progress_callback is not None:
            from agent.display import build_tool_preview
            self.tool_progress_callback("tool.started", name, build_tool_preview(name, args), args)
        # No tool_start_callback: main's snapshots the local FS for inline diffs; the gateway
        # snapshots before its tool writes and ships ``tool.complete.inline_diff`` instead.

    def _on_tool_complete(self, payload, stream_callback, starts):
        name = payload.get("name") or payload.get("tool_name") or "tool"
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        call_id = payload.get("tool_id") or payload.get("tool_call_id") or ""
        result = payload.get("result")
        duration = payload.get("duration_s")
        if duration is None:
            started = starts.pop(call_id, None)
            duration = (time.monotonic() - started) if started is not None else 0.0
        if self.tool_progress_callback is not None:
            self.tool_progress_callback("tool.completed", name, None, None, duration=duration,
                                        is_error=bool(payload.get("is_error")), result=result)
        if name == "todo":
            self._todo_from_result(result)
        diff = payload.get("inline_diff")
        if isinstance(diff, str) and diff:
            from cli import _cprint
            for line in diff.rstrip("\n").splitlines():
                _cprint(line)
            if name == "delegate_task" and self.tool_complete_callback is not None:
                self.tool_complete_callback(call_id, name, args, result)
        elif self.tool_complete_callback is not None:
            self.tool_complete_callback(call_id, name, args, result)

    def _todo_from_result(self, result):
        import json
        try:
            data = json.loads(result) if isinstance(result, str) else result
        except ValueError:
            return
        if isinstance(data, dict) and isinstance(data.get("todos"), list):
            self._todo_store.items = list(data["todos"])

    def _on_todo(self, payload, stream_callback, starts):
        if isinstance(payload.get("todos"), list):
            self._todo_store.items = list(payload["todos"])

    def _on_thinking(self, payload, stream_callback, starts):
        if self.thinking_callback is not None:
            self.thinking_callback(payload.get("text") or "")

    def _on_reasoning(self, payload, stream_callback, starts):
        if self.reasoning_callback is not None:
            self.reasoning_callback(payload.get("text") or "")

    def _on_reasoning_available(self, payload, stream_callback, starts):
        if self.tool_progress_callback is not None:
            self.tool_progress_callback("reasoning.available", "_thinking", (payload.get("text") or "")[:500], None)

    def _on_notice(self, payload, stream_callback, starts):
        if self.notice_callback is None:
            return
        from agent.credits_tracker import AgentNotice
        self.notice_callback(AgentNotice(text=str(payload.get("text") or ""), level=payload.get("level") or "info",
                                         kind=payload.get("kind") or "sticky", ttl_ms=payload.get("ttl_ms"),
                                         key=payload.get("key"), id=payload.get("id")))

    def _on_notice_clear(self, payload, stream_callback, starts):
        if self.notice_clear_callback is not None:
            self.notice_clear_callback(payload.get("key"))

    def _on_reaction(self, payload, stream_callback, starts):
        if self.reaction_callback is not None:
            self.reaction_callback(payload.get("kind"))

    def _on_print(self, payload, stream_callback, starts):
        self._emit_print(payload.get("text") or "")

    def _emit_print(self, text):
        if self.suppress_status_output:
            return
        (self._print_fn or print)(text)

    def _on_approval(self, payload, stream_callback, starts):
        from tools.terminal_tool import _get_approval_callback
        callback = _get_approval_callback()
        choices = list(payload.get("choices") or [])
        choice = "deny"
        if callback is not None:
            try:
                choice = callback(payload.get("command") or "", payload.get("description") or "",
                                  allow_permanent="always" in choices, allow_session="session" in choices,
                                  smart_denied=False)
            except TypeError:
                choice = callback(payload.get("command") or "", payload.get("description") or "")
        if choice not in choices:
            choice = "deny"
        self.connection.call("approval.respond", session_id=self.session_id,
                             execution_generation=payload.get("execution_generation", self._generation),
                             prompt_id=payload.get("prompt_id"), choice=choice)

    def _on_clarify(self, payload, stream_callback, starts):
        answer = ""
        if self.clarify_callback is not None:
            qid = payload.get("prompt_id") or "q0"
            reply = self.clarify_callback([{"qid": qid, "question": payload.get("question") or "",
                                            "choices": list(payload.get("choices") or []),
                                            "multi_select": bool(payload.get("multi_select"))}])
            if isinstance(reply, dict):
                value = (reply.get("answers") or {}).get(qid)
                answer = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else (value or "")
            elif isinstance(reply, str):
                answer = reply
        self.connection.call("clarify.respond", session_id=self.session_id,
                             execution_generation=payload.get("execution_generation", self._generation),
                             prompt_id=payload.get("prompt_id"), answer=str(answer))

    # -- result --------------------------------------------------------------------------------
    def _user_row(self, user_message):
        staged = self._pending_cli_user_message
        if isinstance(staged, dict):
            return staged
        return {"role": "user", "content": user_message}

    def _failed(self, history, user_message, error):
        return {**_RESULT_DEFAULTS, "final_response": "", "messages": history + [self._user_row(user_message)],
                "failed": True, "completed": False, "error": error, "session_id": self.session_id}

    def _result(self, history, user_message, complete):
        receipt = {}
        if self._admission:
            try:
                receipt = self.connection.call("prompt.receipt", session_id=self.session_id,
                                               admission_id=self._admission, include_result=True) or {}
            except GatewayClientError as exc:
                logger.debug("prompt.receipt failed: %s", exc)
        stored = dict(receipt.get("result") or {})
        usage = dict(receipt.get("usage") or {})
        status = (complete or {}).get("status") or (complete or {}).get("outcome")
        out = {**_RESULT_DEFAULTS, **stored}
        if "final_response" not in stored:
            out["final_response"] = (complete or {}).get("text") or (complete or {}).get("content") or ""
        if status in {"interrupted", "cancelled"} or (complete or {}).get("outcome") == "cancelled":
            out["interrupted"] = True
            out["completed"] = False
        elif status in {"error", "failed"} and not stored:
            out.update(failed=True, completed=False)
        suffix = stored.get("messages") if isinstance(stored.get("messages"), list) else None
        if suffix is not None and stored.get("_compressed"):
            out["messages"] = list(suffix)
        else:
            out["messages"] = history + [self._user_row(user_message)] + list(suffix or [])
        if not suffix and out.get("final_response") and not out.get("failed"):
            out["messages"].append({"role": "assistant", "content": out["final_response"]})
        out["session_id"] = self.session_id
        self._apply_usage({**stored, **usage})
        self._session_messages = out["messages"]
        return out

    def _apply_usage(self, usage):
        for attr, key in _COUNTER_FROM_USAGE.items():
            if isinstance(usage.get(key), int):
                setattr(self, attr, usage[key])
        if isinstance(usage.get("total_tokens"), int):
            self.session_total_tokens = usage["total_tokens"]
        else:
            self.session_total_tokens = self.session_prompt_tokens + self.session_completion_tokens
        if isinstance(usage.get("api_calls"), int):
            self.session_api_calls += usage["api_calls"]
        if isinstance(usage.get("last_prompt_tokens"), int):
            self.context_compressor.last_prompt_tokens = usage["last_prompt_tokens"]
        if usage.get("context_length"):
            self.context_compressor.context_length = int(usage["context_length"])
        if usage.get("model"):
            self.model = usage["model"]


def _moa_handler(kind):
    def handler(agent, payload, stream_callback, starts):
        if agent.tool_progress_callback is None:
            return
        extra = {k: v for k, v in payload.items() if k not in {"label", "text", "name", "preview"}}
        agent.tool_progress_callback(kind, payload.get("label") or payload.get("name"),
                                     payload.get("text") or payload.get("preview"), None, **extra)
    return handler


_HANDLERS = {
    "message.delta": GatewayAgent._on_delta,
    "message.interim": GatewayAgent._on_interim,
    "tool.generating": GatewayAgent._on_tool_generating,
    "tool.start": GatewayAgent._on_tool_start,
    "tool.complete": GatewayAgent._on_tool_complete,
    "todo.updated": GatewayAgent._on_todo,
    "thinking.delta": GatewayAgent._on_thinking,
    "reasoning.delta": GatewayAgent._on_reasoning,
    "reasoning.available": GatewayAgent._on_reasoning_available,
    "moa.reference": _moa_handler("moa.reference"),
    "moa.aggregating": _moa_handler("moa.aggregating"),
    "notification.show": GatewayAgent._on_notice,
    "notification.clear": GatewayAgent._on_notice_clear,
    "reaction": GatewayAgent._on_reaction,
    "agent.print": GatewayAgent._on_print,
    "approval.request": GatewayAgent._on_approval,
    "clarify.request": GatewayAgent._on_clarify,
}
