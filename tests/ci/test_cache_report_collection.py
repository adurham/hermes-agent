"""Exercise Actions log collection through the existing report/comment path."""
from __future__ import annotations

import json
import threading

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from scripts.ci import assemble_review_comment, timings_report


@pytest.mark.parametrize("event,is_main", [("pull_request", False), ("push", True), ("workflow_dispatch", True)])
def test_collected_cache_data_reaches_reports_without_forwarding_auth(monkeypatch, event, is_main):
    timestamp = "2026-10-20T12:00:01Z "
    messages = [
        "##[group]Run actions/cache/restore@fixture", "with:",
        "  path: /cache", "  key: fixture-v1-current", "##[endgroup]",
        "Cache hit for restore-key: fixture-v1-previous",
        "Received 100 of 100 (100.0%), 1.0 MBs/sec",
        "Cache Size: ~0 MB (100 B)", "Cache restored successfully",
        "Cache restored from key: fixture-v1-previous",
    ]
    log = "\n".join(timestamp + line for line in messages).encode()
    jobs = [{
        "id": number, "name": name, "status": "completed", "conclusion": conclusion,
        "started_at": "2026-10-20T12:00:00Z", "completed_at": "2026-10-20T12:00:02Z",
        "html_url": f"https://github.com/fixture/cache/actions/runs/42/job/{number}",
        "steps": [{"name": "Cache artifacts", "number": 1, "status": "completed", "conclusion": "success",
                   "started_at": "2026-10-20T12:00:00Z", "completed_at": "2026-10-20T12:00:02Z"}],
    } for number, name, conclusion in [(101, "working", "success"), (102, "logs unavailable", "success"),
                                       (103, "not selected", "skipped")]]
    prefix = "/repos/fixture/cache/actions"
    routes = {
        f"{prefix}/runs/42": (200, {}, json.dumps({"created_at": "2026-10-20T12:00:00Z", "head_branch": "main", "event": event}).encode()),
        f"{prefix}/runs/42/jobs": (200, {}, json.dumps({"jobs": jobs}).encode()),
        f"{prefix}/runs": (200, {}, b'{"workflow_runs": []}'),
        "/blob": (200, {}, log),
    }
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            requests.append((path, self.headers.get("Authorization")))
            status, headers, body = routes.get(path, (404, {}, b"missing"))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    address = f"http://127.0.0.1:{server.server_port}"
    routes[f"{prefix}/jobs/101/logs"] = (302, {"Location": address + "/blob"}, b"")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(timings_report, "API_BASE", address)
    try:
        current = timings_report.collect_timings("fixture-token", "fixture/cache", "42", "head")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert "cache_data" in current, "CI collection must retain per-cache data in the baseline JSON"
    assert current["is_main"] is is_main, "a PR from a branch named main is not a main run"
    caches = current["cache_data"]
    assert caches["unavailable_jobs"] == ["logs unavailable"]
    assert len(caches["entries"]) == 1
    assert caches["entries"][0]["restored_bytes"] == 100
    assert ("/blob", None) in requests, "the signed log-blob hop must not receive the API bearer"
    assert (f"{prefix}/jobs/101/logs", "Bearer fixture-token") in requests
    assert not any("/jobs/103/" in path for path, _ in requests)

    old_baseline = {"jobs": current["jobs"]}
    statuses = timings_report.generate_review_status(current, old_baseline, "https://example.invalid/report")
    cache_status, = [s for s in statuses if s["source"] == "ci caches"]
    assert cache_status["results"][0]["link"] == "https://example.invalid/report"
    comment = assemble_review_comment.assemble(review_statuses_json=json.dumps(statuses))
    assert "CI caches" in comment and "logs unavailable" in comment
    assert "Cache artifacts" in timings_report.generate_html(current, old_baseline)
    assert "#step:1:1" in comment
    assert "CI caches" in timings_report.generate_summary(current, old_baseline)
