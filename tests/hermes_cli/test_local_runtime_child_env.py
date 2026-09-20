"""A managed llama-server child never inherits credential-shaped environment variables.

Issue #116109: on Windows the bundled libomp.dll crashed with STATUS_HEAP_CORRUPTION during
initialisation whenever one `*_API_KEY` variable was present in the inherited Desktop environment
and loaded fine with only that variable removed. Credentials have no business in a native
inference child regardless of the crash, so spawn_server scrubs them at the one spawn boundary.
"""

import json
import os
import subprocess
import sys

from hermes_cli.local_runtime.processes import server_child_env, spawn_server


def test_spawn_server_child_sees_no_credentials_but_keeps_runtime_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-secret")
    monkeypatch.setenv("SOME_TOKEN", "t")
    monkeypatch.setenv("DB_PASSWORD", "p")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    out = tmp_path / "env.json"
    proc, job = spawn_server(
        [sys.executable, "-c",
         "import json, os, sys; json.dump(dict(os.environ), open(sys.argv[1], 'w'))", str(out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        assert proc.wait(timeout=60) == 0
    finally:
        if job is not None:
            job.close()
    child_env = json.loads(out.read_text(encoding="utf-8"))
    assert "FIRECRAWL_API_KEY" not in child_env
    assert "SOME_TOKEN" not in child_env
    assert "DB_PASSWORD" not in child_env
    assert child_env["CUDA_VISIBLE_DEVICES"] == "0"
    assert child_env["OMP_NUM_THREADS"] == "4"
    assert child_env["PATH"] == os.environ["PATH"]


def test_server_child_env_is_case_insensitive_and_pure():
    base = {"openai_api_key": "k", "Path": "C:\\x", "GITHUB_TOKEN": "g", "HSA_OVERRIDE_GFX_VERSION": "11"}
    assert server_child_env(base) == {"Path": "C:\\x", "HSA_OVERRIDE_GFX_VERSION": "11"}
    assert base["openai_api_key"] == "k"  # input untouched
