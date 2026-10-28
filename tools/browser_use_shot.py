"""Screenshot and tab helpers that browser_exec installs over browser-harness's own.

Chrome draws no compositor frame for a hidden tab, and some remotes (Browser Use cloud) never answer
``Page.captureScreenshot`` with ``format: "png"`` for one, so the harness's ``capture_screenshot()`` hangs
for its full 60 s IPC timeout. Its ``new_tab()`` and its daemon both create tabs with ``background=True``,
so the tab the agent drives is often hidden. browser_harness is a pinned third-party dependency; this
module is loaded by file path inside the harness process (``harness_command()``), so it imports nothing
from Hermes and touches the harness only through the helpers module it is given.
"""

from __future__ import annotations

import base64
import io
import os
import tempfile
from typing import Any, Callable

# A png that has not arrived by then will not: a visible tab answers in well under a second, a hidden
# one on Browser Use cloud never does. jpeg on the same hidden tab answers in a few seconds.
PNG_DEADLINE_S = 10.0
JPEG_DEADLINE_S = 60.0
_HARNESS_MODULE = "browser_harness.helpers"


def _default_shot_path() -> str:
    try:
        from browser_harness import _ipc  # the harness's own per-user temp dir
        return str(_ipc._TMP / "shot.png")
    except (ImportError, AttributeError):
        return os.path.join(tempfile.gettempdir(), "shot.png")


def _bring_to_front_if_hidden(cdp: Callable[..., Any], current_tab: Callable[[], dict]) -> None:
    """Activate the attached tab when the page reports it is hidden. A visible tab is left alone, so a
    capture never takes window focus it does not need."""
    try:
        state = cdp("Runtime.evaluate", expression="document.visibilityState", returnByValue=True)
        if (state.get("result") or {}).get("value") != "hidden":
            return
        cdp("Target.activateTarget", targetId=current_tab()["targetId"])
    except Exception:  # an engine without Target.activateTarget (Lightpanda) still captures below
        return


def _write_shot(data: bytes, path: str, fmt: str) -> None:
    """Write ``data`` (encoded as ``fmt``) to ``path``, re-encoding a jpeg as png when the path says png:
    callers and the screenshot embed trust the extension."""
    if fmt == "jpeg" and path.lower().endswith(".png"):
        from PIL import Image
        Image.open(io.BytesIO(data)).save(path, format="PNG")
        return
    with open(path, "wb") as f:
        f.write(data)


def make_capture_screenshot(cdp: Callable[..., Any], current_tab: Callable[[], dict]) -> Callable[..., str]:
    def capture_screenshot(path=None, full=False, max_dim=None):
        """Save a PNG of the current viewport (bringing a hidden tab to the front first). Set
        max_dim=1800 on a 2x display to keep the file under the 2000px-per-side limit some image-aware
        LLMs enforce."""
        path = path or _default_shot_path()
        _bring_to_front_if_hidden(cdp, current_tab)
        fmt = "png"
        try:
            r = cdp("Page.captureScreenshot", _response_timeout=PNG_DEADLINE_S,
                    format="png", captureBeyondViewport=full)
        except TimeoutError:
            fmt = "jpeg"
            try:
                r = cdp("Page.captureScreenshot", _response_timeout=JPEG_DEADLINE_S,
                        format="jpeg", quality=90, captureBeyondViewport=full)
            except TimeoutError as e:
                raise RuntimeError(
                    f"Page.captureScreenshot timed out (png after {PNG_DEADLINE_S:g}s, "
                    f"jpeg after {JPEG_DEADLINE_S:g}s)") from e
        _write_shot(base64.b64decode(r["data"]), path, fmt)
        if max_dim:
            from PIL import Image
            img = Image.open(path)
            if max(img.size) > max_dim:
                img.thumbnail((max_dim, max_dim))
                img.save(path)
        return path
    return capture_screenshot


def make_new_tab(new_tab: Callable[..., Any], cdp: Callable[..., Any]) -> Callable[..., Any]:
    def new_tab_in_front(url="about:blank"):
        """Open (or reuse a blank) tab, navigate to url, and bring it to the front."""
        tid = new_tab(url)
        if tid:
            cdp("Target.activateTarget", targetId=tid)
        return tid
    return new_tab_in_front


def _is_harness_helper(fn: Any) -> bool:
    """True for the harness's own helper; a same-named function from the agent's workspace
    ``agent_helpers.py`` (loaded into the helpers module at import) is the agent's choice."""
    return callable(fn) and getattr(fn, "__module__", "") == _HARNESS_MODULE


def install(helpers: Any, activate_new_tabs: bool = False) -> None:
    """Rebind the helpers on the ``browser_harness.helpers`` module before ``browser_harness.run`` star-imports
    them, so the agent's code, the harness's step tracer and recorder all see these. ``activate_new_tabs`` is
    for browsers no human is watching (cloud providers): there a background tab only costs throttling and
    blank screenshots, while on a user's own Chrome the harness deliberately leaves the visible tab alone."""
    if _is_harness_helper(helpers.capture_screenshot):
        helpers.capture_screenshot = make_capture_screenshot(helpers.cdp, helpers.current_tab)
    if activate_new_tabs and _is_harness_helper(helpers.new_tab):
        helpers.new_tab = make_new_tab(helpers.new_tab, helpers.cdp)


_HARNESS_MODULE_ARGS = ["-m", "browser_harness.run"]
_LAUNCHER = """\
import importlib.util, runpy
spec = importlib.util.spec_from_file_location('hermes_browser_use_shot', {path!r})
shot = importlib.util.module_from_spec(spec); spec.loader.exec_module(shot)
import browser_harness.helpers
shot.install(browser_harness.helpers, activate_new_tabs={activate!r})
runpy.run_module('browser_harness.run', run_name='__main__', alter_sys=True)
"""


def harness_command(cmd: list[str], activate_new_tabs: bool) -> list[str]:
    """``python -m browser_harness.run`` as a launcher that installs these helpers first. The model's code
    still arrives untouched on stdin (no preamble shifting its traceback line numbers). This file is loaded
    by path: the harness interpreter has no Hermes checkout on sys.path."""
    if cmd[1:] != _HARNESS_MODULE_ARGS:
        return cmd
    return [cmd[0], "-c", _LAUNCHER.format(path=os.path.abspath(__file__), activate=bool(activate_new_tabs))]
