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


if __name__ == "__main__":
    unittest.main()
