"""Regression tests for re-queuing interrupt messages that carry images.

Bug: interrupting a running prompt with a NEW message that contained an
attached image silently dropped the new prompt. The image submit path bundles
input as a ``(text, [Path, ...])`` tuple, but the interrupt re-queue logic did
``"\\n".join(all_parts)`` — which raises ``TypeError`` the moment a part is a
tuple. The exception was swallowed by the surrounding handler, so the agent
stopped but never processed the interrupting message.

The fix combines parts structurally via ``_combine_interrupt_parts``, returning
the same ``(text, images)`` tuple / plain-``str`` shapes the ``_pending_input``
consumer already unpacks.
"""

import queue
import unittest
from pathlib import Path


def _import_cli():
    import hermes_cli.config as config_mod

    if not hasattr(config_mod, "save_env_value_secure"):
        config_mod.save_env_value_secure = lambda key, value: {
            "success": True,
            "stored_as": key,
            "validated": False,
        }

    import cli as cli_mod

    return cli_mod


class TestCombineInterruptParts(unittest.TestCase):
    def setUp(self):
        self.combine = _import_cli().HermesCLI._combine_interrupt_parts

    def test_plain_text_only_returns_str(self):
        out = self.combine(["hello", "world"])
        self.assertEqual(out, "hello\nworld")
        self.assertIsInstance(out, str)

    def test_single_image_tuple_preserved(self):
        """The core bug: a lone image-bearing interrupt must NOT crash and must
        keep its image."""
        img = Path("/tmp/shot.png")
        out = self.combine([("look at this", [img])])
        self.assertEqual(out, ("look at this", [img]))

    def test_image_tuple_does_not_raise_typeerror(self):
        # Direct guard against the original "\n".join(tuple) TypeError.
        try:
            self.combine([("caption", [Path("/tmp/a.png")])])
        except TypeError as exc:  # pragma: no cover - failure path
            self.fail(f"_combine_interrupt_parts raised TypeError: {exc}")

    def test_mixed_text_and_image_parts_merge(self):
        img1 = Path("/tmp/1.png")
        img2 = Path("/tmp/2.png")
        out = self.combine([
            "first plain",
            ("second with image", [img1]),
            ("third with image", [img2]),
        ])
        self.assertEqual(out, ("first plain\nsecond with image\nthird with image", [img1, img2]))

    def test_image_only_tuple_empty_text(self):
        img = Path("/tmp/only.png")
        out = self.combine([("", [img])])
        self.assertEqual(out, ("", [img]))

    def test_multiple_images_in_one_part(self):
        imgs = [Path("/tmp/a.png"), Path("/tmp/b.png")]
        out = self.combine([("two shots", imgs)])
        self.assertEqual(out, ("two shots", imgs))

    def test_empty_parts_skipped(self):
        out = self.combine(["", "real", ""])
        self.assertEqual(out, "real")

    def test_result_shape_matches_pending_input_consumer(self):
        """The returned shape must be exactly what the process loop unpacks:
        a tuple unpacks to (text, images); a str leaves images empty."""
        # tuple form
        text_payload, images = self.combine([("x", [Path("/tmp/x.png")])])
        self.assertEqual(text_payload, "x")
        self.assertEqual(images, [Path("/tmp/x.png")])
        # str form — mirrors `if isinstance(user_input, tuple): ... else str`
        out = self.combine(["just text"])
        self.assertIsInstance(out, str)


class _FakeTurn:
    def __init__(self, interrupt_message):
        self.result = {
            "final_response": "partial answer",
            "interrupted": True,
            "interrupt_message": interrupt_message,
            "completed": False,
        }
        self.use_streaming_tts = False


def _make_render_cli():
    """Minimal HermesCLI-shaped object exercising the REAL ``_chat_render_turn``.

    Only display/audio side effects are neutered; the interrupt re-queue block
    under test runs unmodified.
    """
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin

    class _RenderCLI(CLIChatTurnMixin):
        def __init__(self):
            self._interrupt_queue = queue.Queue()
            self._pending_input = queue.Queue()
            self._voice_tts = False
            self._voice_continuous = False
            self._last_turn_interrupted = False
            self.agent = type(
                "_A", (), {"max_iterations": 500, "interrupt_requested": False}
            )()

        def _emit_focus_recovery_line(self):
            pass

        def _ring_bell(self, context=None):
            pass

        def _chat_print_reasoning_box(self, turn):
            pass

        def _chat_print_response_panel(self, turn, response):
            pass

        def _voice_speak_response_async(self, response):
            pass

    return _RenderCLI()


class TestInterruptRequeueAtCallSite(unittest.TestCase):
    """Call-site regression guard for ``_chat_render_turn``'s re-queue block.

    The v2026.9.14 merge moved this call site into ``cli_chat_turn_mixin`` and
    silently reverted it to a bare ``"\\n".join(all_parts)``, reintroducing the
    original crash. The pre-existing tests in this file only exercised
    ``_combine_interrupt_parts`` in ISOLATION, so they stayed green throughout
    the regression — which is exactly why it went unnoticed. These tests drive
    the real method instead.
    """

    def test_mixed_text_and_image_interrupts_requeue_without_crash(self):
        """Two interrupts in quick succession: one plain text, one with an image."""
        cli = _make_render_cli()
        img = Path("/tmp/screenshot.png")
        cli._interrupt_queue.put(("look at this error", [img]))
        turn = _FakeTurn("stop")

        # Must not raise TypeError: sequence item N: expected str instance, tuple found
        cli._chat_render_turn(turn, agent_thread=None, interrupt_msg="stop")

        self.assertFalse(
            cli._pending_input.empty(),
            "interrupt message was silently dropped — nothing re-queued",
        )
        payload = cli._pending_input.get_nowait()
        self.assertIsInstance(payload, tuple)
        text, images = payload
        self.assertEqual(text, "stop\nlook at this error")
        self.assertEqual(images, [img], "the attached image must survive the re-queue")

    def test_single_image_interrupt_requeues_as_tuple(self):
        cli = _make_render_cli()
        img = Path("/tmp/only.png")
        turn = _FakeTurn(("caption", [img]))

        cli._chat_render_turn(turn, agent_thread=None, interrupt_msg=("caption", [img]))

        payload = cli._pending_input.get_nowait()
        self.assertEqual(payload, ("caption", [img]))

    def test_plain_text_interrupts_still_requeue_as_str(self):
        """Unchanged behavior guard: the text-only path must stay a plain str."""
        cli = _make_render_cli()
        cli._interrupt_queue.put("and another thing")
        turn = _FakeTurn("stop")

        cli._chat_render_turn(turn, agent_thread=None, interrupt_msg="stop")

        payload = cli._pending_input.get_nowait()
        self.assertIsInstance(payload, str)
        self.assertEqual(payload, "stop\nand another thing")

    def test_multiple_image_interrupts_concatenate_in_order(self):
        cli = _make_render_cli()
        img1, img2 = Path("/tmp/1.png"), Path("/tmp/2.png")
        cli._interrupt_queue.put(("second", [img2]))
        turn = _FakeTurn(("first", [img1]))

        cli._chat_render_turn(turn, agent_thread=None, interrupt_msg=("first", [img1]))

        text, images = cli._pending_input.get_nowait()
        self.assertEqual(text, "first\nsecond")
        self.assertEqual(images, [img1, img2])


if __name__ == "__main__":
    unittest.main()
