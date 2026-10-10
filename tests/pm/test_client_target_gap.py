"""A by-design target gap refuses client-side — the worker is never spawned.

For a package its registry definition says the target cannot host (e.g. ``git``
on POSIX: "POSIX uses system git by choice"), the worker's ``_install`` can only
fail — and the worker's startup would otherwise import app config inside PM's
minimal runtime (a provider-plugin scan with no dependency tree there).
"""

import pytest

from pm.package import InstallError


def _isolate_store(monkeypatch, tmp_path):
    """Keep PM store resolution inside the test.

    The real checkout lives under the user's hermes home; store resolution
    probes ``repo_root().parent/manifest.json``, which the repo's home-I/O
    guard refuses in tests.
    """
    import pm.paths as paths

    store = tmp_path / "store"
    store.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "store_root", lambda: store)
    monkeypatch.setattr(paths, "writable_store_root", lambda: store)


@pytest.mark.platforms("posix")
def test_missing_by_design_package_refuses_without_spawning_the_worker(monkeypatch):
    from pm import client
    from pm import install as install_mod
    from pm.registry import get_package
    from pm.store import current_target

    worker_requests = []
    monkeypatch.setattr(client, "is_runtime", lambda: False)
    monkeypatch.setattr(install_mod, "is_installed", lambda name: False)
    monkeypatch.setattr(install_mod, "lazy_installs_allowed", lambda: True)
    monkeypatch.setattr(client, "_request", lambda *a, **k: worker_requests.append(a))

    target = current_target()
    reason = get_package("git").missing_reason(target)
    assert reason is not None  # the POSIX gap this test pins

    with pytest.raises(InstallError) as excinfo:
        client.ensure("git")

    assert worker_requests == []
    # Byte-identical to what the worker's _install would have raised.
    assert (excinfo.value.package, excinfo.value.cause, excinfo.value.remedy) == (
        "git", f"unavailable on {target}: {reason}", "none")


@pytest.mark.platforms("posix")
def test_explicit_request_keeps_the_worker_path(tmp_path, monkeypatch):
    from pm import client
    from pm import install as install_mod

    _isolate_store(monkeypatch, tmp_path)
    worker_requests = []
    monkeypatch.setattr(client, "is_runtime", lambda: False)
    monkeypatch.setattr(install_mod, "lazy_installs_allowed", lambda: True)
    monkeypatch.setattr(client, "_request",
                        lambda *a, **k: worker_requests.append(a) or {"result": None})

    client.ensure("git", explicit=True)

    assert worker_requests  # explicit installs stay on the full worker path


def test_missing_but_hostable_package_still_reaches_the_worker(tmp_path, monkeypatch):
    from pm import client
    from pm import install as install_mod

    _isolate_store(monkeypatch, tmp_path)
    worker_requests = []
    monkeypatch.setattr(client, "is_runtime", lambda: False)
    monkeypatch.setattr(install_mod, "is_installed", lambda name: False)
    monkeypatch.setattr(install_mod, "lazy_installs_allowed", lambda: True)
    monkeypatch.setattr(install_mod, "current_target", lambda: "darwin-arm64")
    monkeypatch.setattr(client, "_request",
                        lambda *a, **k: worker_requests.append(a) or {"result": None})

    client.ensure("node")  # supported on darwin-arm64: no gap

    assert worker_requests
