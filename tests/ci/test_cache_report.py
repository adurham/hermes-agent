"""Compressed archive accounting follows cache receipts, not progress or key hashes."""

from __future__ import annotations

import importlib


def _cache(name, key, events, *, action="actions/cache", minute=0):
    marker = f"##[start-action display={name};id=forward.{name}]" if name else ""
    lines = [marker, f"##[group]Run {action}@55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
             "with:", f"  key: {key}", "  lookup-only: false", "##[endgroup]", *events]
    if name:
        lines.append(f"##[end-action id=forward.{name};outcome=success]")
    return "\n".join(f"2026-10-20T20:{minute:02d}:10.1234567Z {line}" for line in lines if line)


def _post(name, events):
    return "\n".join([
        "Post job cleanup.",
        f"##[start-action display={name};id=__changed-uuid.{name}]",
        "Post job cleanup.", *events,
        f"##[end-action id=__changed-uuid.{name};outcome=success]",
    ])


def test_cache_invocations_account_only_confirmed_phase_local_archives():
    report = importlib.import_module("scripts.ci.cache_report")
    first = "tools-" + "a" * 64
    second = "tools-" + "b" * 64
    log = "\n".join([
        _cache("PM tools", first, [
            "Cache hit for restore-key: tools-main",
            "Received 0 of 100 (0.0%), 0.0 MBs/sec",
            "Received 100 of 100 (100.0%), 1.0 MBs/sec",
            "Cache Size: ~0 MB (100 B)", "Cache restored successfully",
            "Cache restored from key: tools-main",
        ]),
        _cache("PM tools", second, [f"Cache not found for input keys: {second}"]),
        _cache("uv", "uv-key", [
            "Cache hit for: uv-key", "Cache Size: ~0 MB (40 B)",
            "Cache restored successfully", "Cache restored from key: uv-key",
        ]),
        _cache("npm", "npm-key", [
            "Cache found and can be restored from key: npm-key", "Lookup only - skipping download",
        ]),
        _cache("unknown size", "unknown-key", ["Cache restored from key: unknown-key"]),
        _cache("transport", "failed-key", [
            "##[warning]Failed to restore: service unavailable",
            "Cache not found for input keys: failed-key",
        ]),
        _cache("race", "race-key", ["Cache not found for input keys: race-key"]),
        _cache("failed restore", "broken-key", [
            "Cache hit for: broken-key", "Received 90 of 90 (100.0%), 1.0 MBs/sec",
            "Cache Size: ~0 MB (90 B)", "##[warning]Failed to restore: tar failed",
        ]),
        _cache(None, "modules-key", [
            "Cache hit for: modules-key", "Cache Size: ~0 MB (7 B)",
            "Cache restored from key: modules-key",
        ], minute=1),
        _cache("unconfirmed", "unconfirmed-key", ["Cache not found for input keys: unconfirmed-key"], minute=2),
        "##[group]Run echo 'Cache saved with key: fake'",
        "echo 'Cache Size: ~0 MB (99999 B)'", "##[endgroup]",
        "Cache saved with key: fake",  # Shell output outside a cache action is not a receipt.
        _post("PM tools", [
            "Sent 0 of 200 (0.0%), 0.0 MBs/sec", "Sent 200 of 200 (100.0%), 1.0 MBs/sec",
            f"Cache saved with key: {second}",
        ]),
        _post("PM tools", [
            "Sent 150 of 150 (100.0%), 1.0 MBs/sec", f"Cache saved with key: {first}",
        ]),
        _post("uv", ["Cache hit occurred on the primary key uv-key, not saving cache."]),
        _post("race", [
            "Sent 500 of 500 (100.0%), 1.0 MBs/sec",
            "##[warning]Failed to save: Unable to reserve cache with key race-key, another job may be creating this cache.",
        ]),
        _post("unknown size", ["Cache saved with key: unknown-key"]),
        _post("unconfirmed", ["Sent 300 of 300 (100.0%), 1.0 MBs/sec"]),
        _post("Upload artifact", ["##[warning]unrelated cleanup warning"]),
    ])
    steps = [{"name": "Restore node_modules", "number": 9, "started_at": "2026-10-20T20:01:10Z",
              "completed_at": "2026-10-20T20:01:11Z"}]
    entries = report.parse_cache_log(log, steps)
    assert len(entries) == 10
    assert [(e["requested_key"], e["matched_key"], e["restore_status"], e["restored_bytes"],
             e["save_status"], e["saved_bytes"]) for e in entries] == [
        (first, "tools-main", "fallback", 100, "saved", 150),
        (second, None, "miss", None, "saved", 200),
        ("uv-key", "uv-key", "exact", 40, "skipped-exact-hit", None),
        ("npm-key", "npm-key", "lookup-only", None, "not-attempted", None),
        ("unknown-key", "unknown-key", "exact", None, "saved", None),
        ("failed-key", None, "miss", None, "not-attempted", None),
        ("race-key", None, "miss", None, "failed", None),
        ("broken-key", "broken-key", "failed", None, "not-attempted", None),
        ("modules-key", "modules-key", "exact", 7, "not-attempted", None),
        ("unconfirmed-key", None, "miss", None, "unknown", None),
    ]
    assert "service unavailable" in entries[5]["warnings"][0]
    assert "Unable to reserve" in entries[6]["warnings"][0]
    assert "tar failed" in entries[7]["warnings"][0]
    assert entries[8]["name"] == "Restore node_modules"
    assert len({e["identity"] for e in entries}) == len(entries)
    changed = report.parse_cache_log(log.replace(first, "tools-" + "c" * 64)
                                    .replace(second, "tools-" + "d" * 64), steps)
    assert [e["identity"] for e in changed] == [e["identity"] for e in entries]




def test_restore_only_and_combined_actions_keep_the_same_comparison_identity():
    report = importlib.import_module("scripts.ci.cache_report")
    main = report.parse_cache_log("\n".join([
        _cache("Cache verified PM tools", "main-hash", [
            "Cache hit for: main-hash", "Cache Size: ~0 MB (100 B)",
            "Cache restored from key: main-hash",
        ]),
        _post("Cache verified PM tools", [
            "Cache hit occurred on the primary key main-hash, not saving cache.",
        ]),
    ]))
    current = report.parse_cache_log("\n".join([
        _cache("Restore verified PM tools without saving", "pr-hash", [
            "Cache hit for: pr-hash", "Cache Size: ~0 MB (120 B)",
            "Cache restored from key: pr-hash",
        ], action="actions/cache/restore"),
        _cache("Save verified PM tools", "pr-hash", [
            "Sent 150 of 150 (100.0%), 1.0 MBs/sec", "Cache saved with key: pr-hash",
        ], action="actions/cache/save"),
    ]))
    assert len(current) == 1
    assert current[0]["identity"] == main[0]["identity"]
    assert current[0]["save_status"] == "saved" and current[0]["saved_bytes"] == 150
    markdown = report.render_cache_summary({"cache_data": {"entries": current}},
                                           {"cache_data": {"entries": main}})
    assert "+20 B" in markdown and "Saved" not in markdown


def test_failed_save_does_not_obscure_a_clean_restore_miss():
    report = importlib.import_module("scripts.ci.cache_report")
    entry = report.parse_cache_log("\n".join([
        _cache("Cache downloads", "key", ["Cache not found for input keys: key"]),
        _post("Cache downloads", ["##[warning]Failed to save: finalization failed"]),
    ]))[0]
    main = dict(entry, restore_status="exact", restored_bytes=10, save_status="skipped-exact-hit")
    markdown = report.render_cache_summary({"is_main": True, "cache_data": {"entries": [entry]}},
                                           {"cache_data": {"entries": [main]}})
    assert "0 / 1 cache hits" in markdown and "1 miss" in markdown
    assert "failed" in markdown and "finalization failed" in markdown
    assert "vs main" not in markdown


def test_content_key_changes_pair_restore_and_save_by_their_transport_paths():
    report = importlib.import_module("scripts.ci.cache_report")
    restore = _cache("Restore signature candidates", "before-build", [
        "Cache hit for restore-key: previous-signatures", "Cache Size: ~0 MB (100 B)",
        "Cache restored from key: previous-signatures",
    ], action="actions/cache/restore").replace("with:", "with:\n  path: /cache/signatures\n/cache/policy")
    save = _cache("Save verified signatures", "after-build", [
        "Sent 140 of 140 (100.0%), 1.0 MBs/sec", "Cache saved with key: after-build",
    ], action="actions/cache/save").replace("with:", "with:\n  path: /cache/signatures\n/cache/policy")
    unrelated = _cache("Save another cache", "another-key", [
        "Sent 10 of 10 (100.0%), 1.0 MBs/sec", "Cache saved with key: another-key",
    ], action="actions/cache/save").replace("with:", "with:\n  path: /cache/other")
    entries = report.parse_cache_log("\n".join([restore, save, unrelated]))
    assert len(entries) == 2, "a content-key change still writes the same restored cache paths"
    assert entries[0]["restored_bytes"] == 100 and entries[0]["saved_bytes"] == 140
    assert entries[0]["saved_key"] == "after-build"
    assert entries[1]["restore_status"] == "not-attempted" and entries[1]["saved_bytes"] == 10
    summary = report.render_cache_summary({"is_main": True, "cache_data": {"entries": entries}}, None)
    assert "140 B" in summary and "100 B" in summary
    assert "vs main" not in summary
