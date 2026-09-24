"""Display and heartbeat phase of the request-local streaming monitor."""

import time
from types import SimpleNamespace
from typing import Optional

from agent import chat_completion_wait_notice as wn
from agent.model_metadata import is_local_endpoint


class StreamingWaitMonitor:
    # FORK: a thinking_delta within this window means thinking is *currently* flowing
    # (``thinking_active`` in the pre-refactor inline heartbeat); mirrors the old
    # block-type state machine's "we are inside a thinking block" semantics.
    _THINKING_ACTIVE_WINDOW = 10.0

    def _anthropic_phase_detail(self, waiting_secs: int, first_chunk_seen: bool) -> Optional[str]:
        """FORK: fine-grained wait phase for the Anthropic wire (or None).

        Refines the coarse two-way ``first_chunk`` / ``post_chunk`` phase with the
        wire-observed signals the pre-refactor inline heartbeat fed to
        ``run_agent._classify_anthropic_stream_phase`` (ping cadence, message_start
        arrival, thinking deltas, content silence). Returns ``None`` — and the caller
        keeps today's coarse phase byte-for-byte — on every non-Anthropic wire and on
        any import problem, so this is strictly additive on the Anthropic streaming
        path. An Anthropic request that simply has not observed a signal yet (no ping,
        no message_start) reports the pre-refactor "queued/prefilling" copy, which is
        the honest reading of that state.

        Signal sources (all already collected for this request):
          * ``_ping_seen`` / ``_ping_count`` / ``_last_ping_time`` — SSE observer in
            ``_StreamingCall._call_anthropic``.
          * ``_message_start_seen`` — same observer (raw ``message_start`` SSE event).
          * ``_last_reasoning_time`` / ``_thinking_chars`` — ``_emit_reasoning``.
          * ``_last_content_time`` — ``_count_chunk`` (pings never advance it).
        """
        try:
            if getattr(self.agent, "api_mode", None) != "anthropic_messages":
                return None
            from run_agent import _classify_anthropic_stream_phase

            now = time.time()
            last_reasoning = float(getattr(self, "_last_reasoning_time", 0.0) or 0.0)
            thinking_active = bool(
                last_reasoning and (now - last_reasoning) <= self._THINKING_ACTIVE_WINDOW
            )
            _thinking_cfg = (self.api_kwargs or {}).get("thinking") or {}
            thinking_requested = bool(
                isinstance(_thinking_cfg, dict)
                and _thinking_cfg.get("type") in ("adaptive", "enabled")
            )
            _last_content = float(getattr(self, "_last_content_time", 0.0) or 0.0) or now
            return _classify_anthropic_stream_phase(
                thinking_active=thinking_active,
                thinking_chars=int(getattr(self, "_thinking_chars", 0) or 0),
                first_event_seen=bool(first_chunk_seen),
                content_silence=int(max(0.0, now - _last_content)),
                thinking_requested=thinking_requested,
                message_start_arrived=bool(getattr(self, "_message_start_seen", False)),
                ping_seen=bool(getattr(self, "_ping_seen", False)),
                user_elapsed=int(waiting_secs),
            )
        except Exception:
            return None

    def _poll_local_load_notice(self, now: float) -> bool:
        """Managed local server: surface a cold model's weight-load progress
        instead of the 60s neutral "waiting on <model>" notice. Polled ~1s only while no
        REAL chunk arrived for 2s+ (never during healthy token flow); in-memory,
        no network. True while loading = heartbeat liveness, skip the rest of
        this iteration (the stale detector's local floor dwarfs any load)."""
        from agent.chat_completion_helpers import _managed_local_load_notice

        m = self._mon
        if now - self.last_chunk_time["t"] < 2.0 or now - m.last_load_poll < 1.0:
            return False
        m.last_load_poll = now
        _load_notice = _managed_local_load_notice(self.agent, self.api_kwargs)
        if _load_notice is not None:
            m.wait_notice_started_ts = None  # The local loader now owns the display.
            m.wait_notice.reset()
            self.agent._emit_wait_notice(_load_notice)
            self.agent._touch_activity("local model loading")
            m.load_notice_shown, m.load_notice_misses, m.last_heartbeat = True, 0, now  # loading IS liveness
            return True
        if m.load_notice_shown:
            # One missed sample is routine (probe timeout under load); clearing on it strobed the line.
            m.load_notice_misses += 1
            if m.load_notice_misses >= 3:
                m.load_notice_shown, m.load_notice_misses = False, 0
                self.agent._emit_wait_notice("")
        return False

    def _heartbeat(self, waiting_secs: int) -> None:
        """Gateway inactivity heartbeat: the start-to-first-chunk gap (thinking,
        local prefill) can exceed the gateway timeout."""
        if waiting_secs >= 60.0:
            # No chunks for 60s+: say WHAT the wait is and WHEN recovery kicks in —
            # once per silence, not every heartbeat (#92550).
            stale = self._stream_stale_timeout
            watchdog = ("stream stale", stale - waiting_secs) if stale is not None and stale != float("inf") else None
            diag = getattr(getattr(self, "clients", None), "diag", None)
            first_chunk_seen = isinstance(diag, dict) and bool(diag.get("first_chunk_at"))
            phase = "post_chunk" if first_chunk_seen else "first_chunk"
            # FORK: refine the phase for the Anthropic wire from the wire-observed
            # signals (the sole consumer of the SSE ping fields). ``phase`` stays the
            # wait-notice template key; the fine phase rides along as a display detail
            # and extends the emit key so a phase transition re-emits exactly once.
            phase_detail = self._anthropic_phase_detail(waiting_secs, first_chunk_seen)
            emit_key = f"{phase}:{phase_detail}" if phase_detail else phase
            if not self._mon.wait_notice.should_emit(emit_key, watchdog):
                self.agent._touch_activity(f"waiting for stream response ({waiting_secs}s, {phase})")
                return
            # FORK: rate-limit signal — tells the user whether a stall is plausibly
            # throttle-related or upstream-only. A hot bucket (>=80%) gets a ⚠ tag;
            # a healthy state collapses to "limits OK (RPM 47/50)", which beats
            # silence-and-guessing. Captured up front from the 200 OK headers, so
            # this lights up immediately rather than waiting for message_start.
            _rl_bit = ""
            try:
                _rl_state = self.agent._rate_limit_state
                if _rl_state and _rl_state.has_data:
                    from agent.rate_limit_tracker import format_rate_limit_heartbeat
                    _fragment = format_rate_limit_heartbeat(_rl_state)
                    if _fragment:
                        _rl_bit = f"; {_fragment}"
            except Exception:
                pass  # Never let display formatting break the heartbeat.
            # FORK: real-evidence diag bits (pre-refactor inline heartbeat format):
            # ping count + last-arrival age. Without these, every long pre-event wait
            # reads identically regardless of whether pings are actually flowing.
            _ping_bit = ""
            try:
                _ping_count = int(getattr(self, "_ping_count", 0) or 0)
                _last_ping = float(getattr(self, "_last_ping_time", 0.0) or 0.0)
                if _last_ping > 0:
                    _age = int(max(0.0, time.time() - _last_ping))
                    _ping_bit = f"; {_ping_count} ping{'s' if _ping_count != 1 else ''}, last {_age}s ago"
            except Exception:
                pass  # Never let display formatting break the heartbeat.
            self._mon.wait_notice_started_ts = self._mon.last_heartbeat
            self.agent._emit_wait_notice(wn.wait_notice_text(
                self.api_kwargs.get('model', 'the provider'), waiting_secs, phase, watchdog)
                + (f" ({phase_detail})" if phase_detail else "") + _ping_bit + _rl_bit)
        else:
            # Chunks are flowing — keep the tracker fresh, leave the display alone.
            self.agent._touch_activity(f"waiting for stream response ({waiting_secs}s, no chunks yet)")

    def _monitor_loop(self) -> None:
        _HEARTBEAT_INTERVAL = 30.0  # seconds between gateway activity touches
        self._mon = SimpleNamespace(
            last_heartbeat=time.time(), last_load_poll=0.0,
            load_notice_shown=False, load_notice_misses=0, wait_notice_started_ts=None,
            wait_notice=wn.WaitNoticeState(),
        )
        _is_local_base = bool(self.agent.base_url) and is_local_endpoint(self.agent.base_url)
        while not self._call_done.is_set():
            self._call_done.wait(timeout=0.3)
            _hb_now = time.time()
            if _is_local_base and self._poll_local_load_notice(_hb_now):
                continue
            # Reasoning callbacks do not clear the classic CLI spinner. The empty
            # protocol payload resets status without adding synthetic reasoning.
            if (self._mon.wait_notice_started_ts is not None
                    and self.last_chunk_time["t"] > self._mon.wait_notice_started_ts):
                self.agent._emit_wait_notice("")
                self._mon.wait_notice_started_ts = None
                self._mon.wait_notice.reset()
            if _hb_now - self._mon.last_heartbeat >= _HEARTBEAT_INTERVAL:
                self._mon.last_heartbeat = _hb_now
                self._heartbeat(int(_hb_now - self.last_chunk_time["t"]))
            _stale_elapsed = time.time() - self.last_chunk_time["t"]
            if _stale_elapsed > self._stream_stale_timeout:
                self._mon.wait_notice_started_ts = None  # Reconnect status has its own owner.
                self._mon.wait_notice.reset()
                self._kill_stale_stream(_stale_elapsed)
            if self.agent._interrupt_requested:
                self._abort_for_interrupt(_stale_elapsed)
                return

