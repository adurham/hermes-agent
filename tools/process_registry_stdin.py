"""Why a background process has no writable stdin, phrased as the fix the agent can act on."""
import importlib.util
import sys


def _pty_available() -> bool:
    return importlib.util.find_spec("winpty" if sys.platform == "win32" else "ptyprocess") is not None


def _pty_relaunch_helps(session) -> bool:
    """A local pipe-mode spawn (stdin is /dev/null) where ``spawn_local`` could give a PTY instead."""
    return session.process is not None and not session.output_log and _pty_available()


def already_exited_error(session) -> str:
    """A pipe-mode process that read stdin got EOF at once and exited: say so, or the agent retries blind."""
    if session.process is None or session.process.stdin is not None or not _pty_relaunch_helps(session):
        return "Process has already finished"
    return ("Process has already finished. It was started without stdin (terminal background=true without "
            "pty=true), so anything it read got end-of-file at once. Relaunch it with "
            "terminal(background=true, pty=true) to send input.")


def stdin_unavailable_error(session) -> str:
    """Error for process_manage write/submit on a session without a stdin pipe or PTY.

    A local pipe-mode spawn connects stdin to /dev/null by design, so only a PTY relaunch can take
    input; that advice is wrong where ``spawn_local`` cannot give a PTY (an isolated worker writes
    output to a log, or the PTY library is missing), and a non-local backend has no live stdin."""
    if session.process is None:
        return ("Process stdin not available: it runs on a non-local terminal backend (or was recovered "
                "after a restart), which has no live stdin. Pass input non-interactively instead "
                "(arguments, a here-doc, or `printf ... | cmd`).")
    if not _pty_relaunch_helps(session):
        return ("This process was started without stdin, and this session cannot give background "
                "processes a pseudo-terminal, so pty=true would not help. Pass input non-interactively "
                "instead (arguments, a here-doc, or `printf ... | cmd`).")
    return ("This process was started without stdin (terminal background=true without pty=true). "
            "Relaunch it with terminal(background=true, pty=true) to send input.")
