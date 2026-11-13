"""The claude-code web provider: WebSearch / WebFetch through a restricted, short-lived ``claude -p`` child.

A fake ``claude`` executable (a Python script on PATH) records exactly how it was launched (argv, stdin, cwd,
env) and answers with canned stream-json, so the tests exercise the real subprocess path: the hardening flags,
stdin-only input, env hygiene, serialization, the timeout kill and the organization-failure breaker.
"""
from __future__ import annotations

import itertools
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

FAKE = r'''#!{python}
import json, os, sys, time
mode = os.environ.get("FAKE_CLAUDE_MODE", "search")
stdin = sys.stdin.read()
rec = {{"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(), "cwd_listing": sorted(os.listdir(".")),
       "env": {{k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DISABLE_TELEMETRY",
                                              "DISABLE_ERROR_REPORTING", "DISABLE_AUTOUPDATER", "NO_PROXY", "EXTRA")}},
       "t_start": time.time()}}
def emit(obj):
    print(json.dumps(obj), flush=True)
def finish(extra=None):
    rec["t_end"] = time.time()
    with open(os.environ["FAKE_CLAUDE_RECORD"], "a") as fh:
        fh.write(json.dumps(rec) + "\n")
if mode == "orgfail":
    finish()
    sys.stderr.write("Unable to verify organization for the current authentication token.\n")
    sys.exit(1)
if mode == "sleep":
    finish()
    time.sleep(60)
    sys.exit(0)
emit({{"type": "system", "subtype": "init"}})
if mode in ("search", "slow", "tool_error"):
    if mode == "slow":
        time.sleep(0.6)
    emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "WebSearch", "input": {{"query": stdin}}}}]}}}})
    links = [{{"title": "Alpha", "url": "https://a.example/1"}}, {{"title": "Beta", "url": "https://b.example/2"}},
             {{"title": "Gamma", "url": "https://c.example/3"}}]
    text = ("Web search results for query: x\n\nLinks: " + json.dumps(links) +
            "\n\nAlpha and Beta agree.\n\nREMINDER: You MUST include the sources")
    emit({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1", "content": text,
                                                   "is_error": mode == "tool_error"}}]}},
          "tool_use_result": {{"query": stdin, "results": [{{"tool_use_id": "s1", "content": links}}]}}}})
elif mode == "fetch":
    for i, url in enumerate(stdin.split()):
        emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "f%d" % i, "name": "WebFetch",
                                                              "input": {{"url": url, "prompt": "p"}}}}]}}}})
        emit({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "f%d" % i,
                                                       "content": "SUMMARY of " + url}}]}},
              "tool_use_result": {{"bytes": 1234, "url": url, "result": "SUMMARY of " + url}}}})
# mode "nosearch": no tool use at all
emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "DONE"}}]}}}})
emit({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": "DONE"}})
finish()
'''


@pytest.fixture()
def provider(tmp_path, monkeypatch):
    from plugins.web.claude_code import provider as mod

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    record = tmp_path / "record.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-reach-the-child")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-must-not-reach-the-child")
    monkeypatch.setattr(mod, "_settings", dict)
    monkeypatch.setattr(mod, "_selected", lambda: True)
    mod._reset_state()
    inst = mod.ClaudeCodeWebProvider()
    inst.record = record  # type: ignore[attr-defined]
    inst.mod = mod  # type: ignore[attr-defined]
    yield inst
    mod._reset_state()


def _records(inst) -> list[dict]:
    path: Path = inst.record
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_is_available_needs_the_binary_and_an_explicit_selection_and_never_spawns(provider, monkeypatch, tmp_path):
    assert provider.is_available() is True
    assert _records(provider) == []
    monkeypatch.setattr(provider.mod, "_selected", lambda: False)  # on PATH but nobody chose it
    assert provider.is_available() is False
    monkeypatch.setattr(provider.mod, "_selected", lambda: True)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert provider.is_available() is False
    assert _records(provider) == []


@pytest.mark.parametrize("key", ["backend", "search_backend", "extract_backend"])
def test_selected_reads_any_web_backend_key(provider, monkeypatch, key):
    import hermes_cli.config as cfg

    monkeypatch.undo()  # the real _selected, not the fixture's stub
    monkeypatch.setattr(cfg, "load_config", lambda: {"web": {key: " Claude-Code "}})
    assert provider.mod._selected() is True
    monkeypatch.setattr(cfg, "load_config", lambda: {"web": {key: "brave-free"}})
    assert provider.mod._selected() is False
    monkeypatch.setattr(cfg, "load_config", dict)
    assert provider.mod._selected() is False


def test_never_auto_selected_by_the_registry_when_unconfigured(provider, monkeypatch):
    """With the binary on PATH and nothing configured, the registry must not hand queries to Claude."""
    import hermes_cli.config as cfg
    from hermes_cli.plugins import _ensure_plugins_discovered

    _ensure_plugins_discovered()
    monkeypatch.undo()
    monkeypatch.setenv("PATH", f"{Path(provider.record).parent / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cfg, "load_config", dict)
    from agent.web_search_registry import get_active_extract_provider, get_active_search_provider

    for active in (get_active_search_provider(), get_active_extract_provider()):
        assert active is None or active.name != "claude-code"


def test_registered_with_both_capabilities(provider):
    assert provider.name == "claude-code"
    assert provider.supports_search() and provider.supports_extract()
    assert provider.get_setup_schema()["env_vars"] == []


def test_search_child_is_locked_down(provider):
    out = provider.search("python venv", limit=2)
    (rec,) = _records(provider)
    argv = rec["argv"]
    assert _arg(argv, "--tools") == "WebSearch" and _arg(argv, "--allowedTools") == "WebSearch"
    assert _arg(argv, "--setting-sources") == ""
    assert _arg(argv, "--permission-mode") == "dontAsk"
    assert _arg(argv, "--output-format") == "stream-json" and "--verbose" in argv
    assert {"--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(argv)
    assert _arg(argv, "--model") == "haiku"
    assert "python venv" not in argv and rec["stdin"] == "python venv"  # the query never rides in argv
    assert rec["cwd"] != os.getcwd() and rec["cwd_listing"] == []  # empty temp dir, not the project
    assert rec["env"]["ANTHROPIC_API_KEY"] is None and rec["env"]["ANTHROPIC_AUTH_TOKEN"] is None
    assert rec["env"]["DISABLE_TELEMETRY"] == "1" and rec["env"]["DISABLE_ERROR_REPORTING"] == "1"
    assert rec["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert out["success"] is True
    assert [r["url"] for r in out["data"]["web"]] == ["https://a.example/1", "https://b.example/2"]  # limit honored
    assert out["data"]["web"][0] == {"title": "Alpha", "url": "https://a.example/1", "description": "", "position": 1}
    assert out["data"]["summary"] == "Alpha and Beta agree."  # REMINDER boilerplate stripped


def test_leading_dash_query_is_not_a_flag(provider):
    provider.search("--dangerously-skip-permissions please")
    (rec,) = _records(provider)
    assert rec["stdin"] == "--dangerously-skip-permissions please"
    assert "--dangerously-skip-permissions" not in rec["argv"]


def test_extract_child_is_locked_down_and_maps_results_by_url(provider, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fetch")
    docs = provider.extract(["https://x.example/a", "https://y.example/b"])
    (rec,) = _records(provider)
    argv = rec["argv"]
    assert _arg(argv, "--tools") == "WebFetch" and _arg(argv, "--allowedTools") == "WebFetch"
    assert _arg(argv, "--setting-sources") == "" and "https://x.example/a" not in argv
    assert rec["stdin"] == "https://x.example/a\nhttps://y.example/b"
    assert [d["url"] for d in docs] == ["https://x.example/a", "https://y.example/b"]
    assert docs[0]["content"] == "SUMMARY of https://x.example/a" and docs[0]["raw_content"] == docs[0]["content"]
    assert docs[0]["metadata"]["summarized"] is True and docs[0]["metadata"]["source"] == "claude-code"
    assert "error" not in docs[0]


def test_extract_reports_urls_that_got_no_result(provider, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nosearch")
    docs = provider.extract(["https://x.example/a"])
    assert docs[0]["url"] == "https://x.example/a" and "no result" in docs[0]["error"]


def test_extract_caps_url_count(provider):
    docs = provider.extract([f"https://x.example/{i}" for i in range(11)])
    assert len(docs) == 11 and all("at most" in d["error"] for d in docs)
    assert _records(provider) == []


def test_empty_query_and_empty_urls(provider):
    assert provider.search("   ")["success"] is False
    assert provider.extract([]) == []
    assert _records(provider) == []


def test_config_env_and_model_apply_but_cannot_undo_quiet_flags(provider, monkeypatch):
    monkeypatch.setattr(provider.mod, "_settings", lambda: {
        "model": "sonnet", "env": {"NO_PROXY": "*", "EXTRA": "1", "DISABLE_TELEMETRY": "0"},
    })
    provider.search("q")
    (rec,) = _records(provider)
    assert _arg(rec["argv"], "--model") == "sonnet"
    assert rec["env"]["NO_PROXY"] == "*" and rec["env"]["EXTRA"] == "1"
    assert rec["env"]["DISABLE_TELEMETRY"] == "1"  # the quiet flags win over config


def test_search_without_a_websearch_result_says_so(provider, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "nosearch")
    out = provider.search("q")
    assert out["success"] is False and "disabled by the organization" in out["error"]


def test_websearch_tool_error_is_reported(provider, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "tool_error")
    out = provider.search("q")
    assert out["success"] is False and "WebSearch failed" in out["error"]


def test_org_verification_failure_opens_the_breaker_and_stops_spawning(provider, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "orgfail")
    first = provider.search("q")
    assert first["success"] is False and "verify the organization" in first["error"] and "not retrying" in first["error"]
    assert len(_records(provider)) == 1
    second = provider.search("q again")
    third = provider.extract(["https://x.example/a"])
    assert second["success"] is False and "not retrying" in second["error"]
    assert "not retrying" in third[0]["error"]
    assert len(_records(provider)) == 1  # no further spawns while the breaker is open
    provider.mod._reset_state()
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "search")
    assert provider.search("recovered")["success"] is True


def test_timeout_kills_the_child_and_reports(provider, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    monkeypatch.setattr(provider.mod, "_timeout", lambda settings, factor=1: 1.0)
    started = time.monotonic()
    out = provider.search("q")
    assert out["success"] is False and "timed out" in out["error"]
    assert time.monotonic() - started < 20


def test_missing_binary_is_a_clean_failure(provider, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    out = provider.search("q")
    assert out["success"] is False and "not found on PATH" in out["error"]


def test_children_are_serialized(provider, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    results: list[dict] = []
    threads = [threading.Thread(target=lambda n=n: results.append(provider.search(f"q{n}"))) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(results) == 3 and all(r["success"] for r in results)
    spans = sorted((r["t_start"], r["t_end"]) for r in _records(provider))
    assert len(spans) == 3
    for (_, end_a), (start_b, _) in itertools.pairwise(spans):
        assert start_b >= end_a - 0.05, "two claude children overlapped"


def test_provider_is_discoverable_through_the_registry(provider):
    from hermes_cli.plugins import _ensure_plugins_discovered

    _ensure_plugins_discovered()
    from agent.web_search_registry import get_provider

    found = get_provider("claude-code")
    assert found is not None and found.supports_search() and found.supports_extract()
