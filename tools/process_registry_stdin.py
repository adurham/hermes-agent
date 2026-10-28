"""Why a background process has no writable stdin, phrased as the fix the agent can act on."""
import importlib.util
import sys


def _pty_available() -> bool:
    return importlib.util.find_spec("winpty" if sys.platform == "win32" else "ptyprocess") is not None


def stdin_unavailable_error(session) -> str:
    """Error for process_manage write/submit on a session without a stdin pipe or PTY.

    A local pipe-mode spawn connects stdin to /dev/null by design, so only a PTY relaunch can take
    input; that advice is wrong where ``spawn_local`` cannot give a PTY (an isolated worker writes
    output to a log, or the PTY library is missing), and a non-local backend has no live stdin."""
    if session.process is None:
        return ("Process stdin not available: it runs on a non-local terminal backend (or was recovered "
                "after a restart), which has no live stdin. Pass input non-interactively instead "
                "(arguments, a here-doc, or `printf ... | cmd`).")
    if session.output_log or not _pty_available():
        return ("This process was started without stdin, and this session cannot give background "
                "processes a pseudo-terminal, so pty=true would not help. Pass input non-interactively "
                "instead (arguments, a here-doc, or `printf ... | cmd`).")
    return ("This process was started without stdin (terminal background=true without pty=true). "
            "Relaunch it with terminal(background=true, pty=true) to send input.")
