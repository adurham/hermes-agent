"""Cache presentation separates read-only comparisons from main's writes."""
from __future__ import annotations

import pytest

from scripts.ci import cache_report as report
from tests.ci.test_cache_report import _cache, _post


def _snapshot(main: bool, *, baseline: bool = False) -> dict:
    log = "\n".join([
        _cache("Cache packages", "hit", [
            "Cache hit for: hit", f"Cache Size: ~0 MB ({80 if baseline else 100} B)",
            "Cache restored from key: hit",
        ]),
        _cache("Cache tools", "miss", ["Cache not found for input keys: miss"]),
        _cache("Cache broken", "broken", ["##[warning]Failed to restore: service | <script>"]),
        _cache("Lookup", "lookup", ["Cache found and can be restored from key: lookup"]),
        _post("Cache packages", [
            f"Sent {90 if baseline else 130} of {90 if baseline else 130} (100.0%), 1.0 MBs/sec",
            "Cache saved with key: hit",
        ]),
        _post("Cache tools", [
            f"Sent {70 if baseline else 40} of {70 if baseline else 40} (100.0%), 1.0 MBs/sec",
            "Cache saved with key: miss",
        ]),
    ])
    steps = [{"name": "Prepare packages", "number": 4, "started_at": "2026-10-20T20:00:00Z",
              "completed_at": "2026-10-20T20:00:59Z"}]
    entries = report.parse_cache_log(log, steps)
    return {"is_main": main, "cache_data": {"entries": [dict(e, job="job|<svg>",
            job_url="https://github.com/fixture/cache/actions/runs/1/job/2") for e in entries],
            "jobs_collected": 1, "unavailable_jobs": ["lost|<script>"]}}


@pytest.mark.parametrize("is_main", [False, True], ids=["pull-request", "main"])
def test_cache_summary_and_expando_follow_the_run_mode(is_main):
    current, baseline = _snapshot(is_main), _snapshot(True, baseline=True)
    result = report.cache_review_status(current, baseline)[0]["results"][0]
    markdown, html = report.render_cache_summary(current, baseline), report.render_cache_html(current, baseline)
    assert "1 / 3 cache hits" in result["summary"]
    assert "1 miss" in result["summary"]
    assert "100 B restored" in result["summary"]
    assert f"{40 if is_main else 70} B not restored" in result["summary"]
    assert "unknown" in result["summary"]
    for rendered in (result["detail"], markdown, html):
        assert "<details>" in rendered and "<summary>" in rendered
        assert "hits" in rendered and "misses" in rendered
        assert "job" in rendered and "Cache packages" in rendered
        assert "#step:4:1" in rendered
        assert "lost" in rendered and "&lt;script&gt;" in rendered
        assert "<script>" not in rendered and "<svg>" not in rendered
    if is_main:
        assert "170 B saved" in result["summary"]
        assert "Saved" in html and "130 B" in html and "40 B" in html
        assert "vs main" not in html and "vs main" not in markdown
        assert "+10 B" not in html
    else:
        assert "saved" not in result["summary"]
        assert "Saved" not in html and "130 B" not in html
        assert "vs main" in html and "+10 B" in html
        misses = result["detail"].split("<h3>misses</h3>", 1)[1].split("<h3>", 1)[0]
        assert "Cache tools" in misses
        assert "Restored" not in misses and "vs main" not in misses


def test_miss_bytes_remain_unknown_without_a_main_reference():
    current = _snapshot(False)
    result = report.cache_review_status(current, None)[0]["results"][0]
    assert "not restored: unknown" in result["summary"]
    assert "0 B not restored" not in result["summary"]
    assert "main-size reference" in result["detail"]
    assert "lookup-only" in result["detail"]
    current["cache_data"]["entries"][0]["restored_bytes"] = None
    unknown = report.cache_review_status(current, None)[0]["results"][0]
    assert "restored: unknown" in unknown["summary"]
    assert "0 B restored" not in unknown["summary"]
