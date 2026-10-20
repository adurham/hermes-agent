"""Cache archive observability from ordinary actions/cache logs (no inventory scans).

Sizes are compressed archive bytes, never unpacked directory or per-file changes.
A progress total is evidence of size, not evidence that a restore/write succeeded.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
import re
from typing import cast


def _record(name: str, action: str) -> dict:
    return {
        "name": name, "action": action, "requested_key": None, "matched_key": None,
        "restore_status": "not-attempted" if action.endswith("/save") else "unknown",
        "restored_bytes": None, "save_status": "not-attempted", "saved_bytes": None,
        "saved_key": None, "warnings": [], "restore_warnings": [], "paths": [],
    }


def _message(raw: str) -> tuple[str | None, str]:
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    match = re.match(r"^(\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-][\d:]+)) (.*)$", raw)
    return (match[1], match[2]) if match else (None, raw)


def _stable_label(name: str) -> str:
    # GitHub uses the pinned action reference as the default top-level step name.
    return re.sub(r"(actions/cache(?:/restore|/save)?)@\S+", r"\1", name.removeprefix("Post "))


class _CacheLogParser:
    def __init__(self, steps: list[dict]):
        self.entries: list[dict] = []
        self.phase: dict | None = None
        self.marker: str | None = None
        self.inputs = False
        self.path_input = False
        self.steps = [
            (datetime.fromisoformat(s["started_at"]).timestamp(),
             datetime.fromisoformat(s["completed_at"]).timestamp() + 1, s["name"])
            for s in steps if s.get("started_at") and s.get("completed_at")
            and s.get("conclusion") != "skipped"
        ]

    def _name(self, timestamp: str | None, action: str) -> str:
        step = None
        if timestamp:
            instant = datetime.fromisoformat(timestamp).timestamp()
            step = next((name for start, end, name in reversed(self.steps)
                         if start <= instant < end), None)
        labels = [_stable_label(label) for label in (step, self.marker) if label]
        return " / ".join(dict.fromkeys(labels)) or action

    def _start(self, timestamp: str | None, action: str, save: bool):
        self.phase = {
            "entry": _record(self._name(timestamp, action), action), "save": save,
            "size": None, "received": None, "sent": None, "restored": False,
            "seen": False,
        }

    def _save_target(self, entry: dict) -> dict | None:
        key = entry["saved_key"] or entry["requested_key"]
        candidates = [e for e in reversed(self.entries) if e["save_status"] == "not-attempted"]
        if key:
            exact = next((e for e in candidates if e["requested_key"] == key), None)
            if exact is not None:
                return exact
        # Content-keyed saves compute a new key after the build. Their identical
        # transport paths identify which restored archive the write replaces.
        if entry["paths"]:
            return next((e for e in candidates if e["paths"] == entry["paths"]), None)
        if key:
            return None
        # Missing-path and other pre-upload failures can have no key in the post log.
        return next((e for e in candidates if e["name"] == entry["name"]), None)

    def _finish(self):
        phase, self.phase = self.phase, None
        self.inputs = False
        self.path_input = False
        if phase is None:
            return
        entry = phase["entry"]
        if phase["save"]:
            if not phase["seen"]:
                return
            if entry["save_status"] == "not-attempted":
                entry["save_status"] = "unknown"
            if entry["save_status"] == "saved":
                entry["saved_bytes"] = phase["sent"] if phase["sent"] is not None else phase["size"]
            target = self._save_target(entry)
            if target:
                for field in ("saved_key", "save_status", "saved_bytes"):
                    target[field] = entry[field]
                target["warnings"].extend(entry["warnings"])
                return
        elif phase["restored"]:
            entry["restore_status"] = (
                "exact" if entry["requested_key"] == entry["matched_key"] else "fallback"
            )
            entry["restored_bytes"] = phase["size"] if phase["size"] is not None else phase["received"]
        elif entry["restore_status"] == "unknown" and entry["warnings"]:
            entry["restore_status"] = "failed"
        self.entries.append(entry)

    def _warning(self, phase: dict, line: str) -> bool:
        match = re.match(r"^(?:##\[(?:warning|error)\]|\[(?:warning|error)\])(.+)$", line)
        message = match[1] if match else line
        failure = message.startswith(("Failed to save:", "Failed to restore:", "Path Validation Error:"))
        if not match and not failure:
            return False
        phase["entry"]["warnings"].append(message)
        if not phase["save"]:
            phase["entry"]["restore_warnings"].append(message)
        # Unrelated actions also emit warnings during Post job cleanup.
        phase["seen"] = phase["seen"] or failure
        if phase["save"] and failure:
            phase["entry"]["save_status"] = "failed"
            key = re.search(r"with key (.+?)(?:,|$)", message)
            if key:
                phase["entry"]["requested_key"] = key[1]
        return True

    def _event(self, phase: dict, line: str):
        if self._warning(phase, line):
            return
        size = re.fullmatch(r"Cache Size: ~[\d.]+ MB \((\d+) B\)", line)
        progress = re.fullmatch(r"(Received|Sent) \d+ of (\d+) \([\d.]+%\), [\d.]+ MBs/sec", line)
        if size:
            phase["size"] = int(size[1])
        if progress:
            phase["received" if progress[1] == "Received" else "sent"] = int(progress[2])
            phase["seen"] = True
        if phase["save"]:
            self._save_event(phase, line)
        else:
            self._restore_event(phase, line)

    def _save_event(self, phase: dict, line: str):
        entry = phase["entry"]
        saved = re.fullmatch(r"Cache saved with key: (.+)", line)
        skipped = re.fullmatch(r"Cache hit occurred on the primary key (.+), not saving cache\.", line)
        if saved or skipped:
            phase["seen"] = True
            entry["saved_key"] = (saved or skipped)[1]
            entry["save_status"] = "saved" if saved else "skipped-exact-hit"

    def _restore_event(self, phase: dict, line: str):
        entry = phase["entry"]
        hit = re.fullmatch(r"Cache hit for(?: restore-key)?: (.+)", line)
        restored = re.fullmatch(r"Cache restored from key: (.+)", line)
        miss = re.fullmatch(r"Cache not found for input keys: (.+)", line)
        lookup = re.fullmatch(r"Cache found and can be restored from key: (.+)", line)
        if hit or restored or lookup:
            entry["matched_key"] = (hit or restored or lookup)[1]
            if entry["requested_key"] is None and hit and "restore-key" not in line:
                entry["requested_key"] = hit[1]
        if restored or line == "Cache restored successfully":
            phase["restored"] = True
        if miss:
            entry["restore_status"] = "miss"
            if entry["requested_key"] is None:
                entry["requested_key"] = miss[1].split(", ")[0]
        if lookup or line == "Lookup only - skipping download":
            entry["restore_status"] = "lookup-only"

    def feed(self, timestamp: str | None, line: str):
        start = re.fullmatch(r"##\[start-action display=(.*);id=[^\]]+\]", line)
        if start:
            self._finish()
            self.marker = start[1]
            return
        if line.startswith("##[end-action"):
            self._finish()
            self.marker = None
            return
        if line == "Post job cleanup.":
            self._finish()
            self._start(timestamp, "actions/cache", save=True)
            return
        if line.startswith("##[group]Run "):
            self._finish()
            action = re.fullmatch(r"##\[group\]Run (actions/cache(?:/restore|/save)?)@\S+", line)
            if action:
                self._start(timestamp, action[1], save=action[1].endswith("/save"))
                self.inputs = True
            else:
                self.marker = None
            return
        if line == "##[endgroup]":
            self.inputs = False
            return
        if self.phase is None:
            return
        if self.inputs:
            path = re.fullmatch(r"  path: ?(.*)", line)
            if path:
                self.phase["entry"]["paths"] = [path[1]] if path[1] else []
                self.path_input = True
            elif re.match(r"  [\w-]+:", line) or line in ("with:", "env:"):
                self.path_input = False
            elif self.path_input and line:
                self.phase["entry"]["paths"].append(line)
            key = re.fullmatch(r"  key: (.+)", line)
            if key:
                self.phase["entry"]["requested_key"] = key[1]
            return
        self._event(self.phase, line)


def parse_cache_log(log: str, steps: list[dict] | None = None) -> list[dict]:
    """Parse invocation-local cache receipts; absent archive sizes remain ``None``.

    Raw GitHub API steps supply top-level labels when composite markers are absent.
    Identities are label + occurrence, independent of content keys and post UUIDs.
    Restore-only and explicit-save actions with the same primary key are paired,
    without applying the combined action's exact-hit save guard to explicit saves.
    """
    parser = _CacheLogParser(steps or [])
    for raw in log.splitlines():
        parser.feed(*_message(raw))
    parser._finish()
    occurrences: dict[str, int] = {}
    for entry in parser.entries:
        # PRs use restore-only PM actions where main uses combined cache actions.
        labels = [re.sub(r"^(?:Cache|Restore|Save) ", "", label).removesuffix(" without saving")
                  for label in entry["name"].split(" / ")]
        name = " / ".join(labels)
        occurrences[name] = occurrences.get(name, 0) + 1
        entry["occurrence"] = occurrences[name]
        entry["identity"] = f"{name} #{entry['occurrence']}"
    return parser.entries


def _bytes(value: int | None, *, signed: bool = False) -> str:
    if value is None:
        return "unknown"
    exact = f"{value:+,}" if signed else f"{value:,}"
    if abs(value) < 1024:
        return f"{exact} B"
    mib = f"{value / (1024 * 1024):+.2f}" if signed else f"{value / (1024 * 1024):.2f}"
    return f"{mib} MiB ({exact} B)"


def _observed(entry: dict | None, phase: str) -> int | None:
    if entry is None:
        return None
    status = entry[f"{phase}_status"]
    if phase == "restore" and status in ("exact", "fallback"):
        return entry["restored_bytes"]
    if phase == "save" and status == "saved":
        return entry["saved_bytes"]
    if (phase, status) == ("save", "skipped-exact-hit"):
        return 0
    if (phase, status) == ("restore", "miss") and not entry.get("restore_warnings", entry["warnings"]):
        return 0
    return None


def _transfer(entry: dict | None, phase: str) -> str:
    if entry is None:
        return "not observed"
    status = entry[f"{phase}_status"]
    return f"{status} · {_bytes(_observed(entry, phase))}"


def _delta(current: dict | None, main: dict | None, phase: str) -> str:
    a, b = _observed(current, phase), _observed(main, phase)
    return _bytes(a - b, signed=True) if a is not None and b is not None else "unknown"


def _archive_delta(entry: dict | None) -> str:
    if entry is None or entry["save_status"] != "saved":
        return "not written" if entry and entry["save_status"] == "skipped-exact-hit" else "unknown"
    saved, restored = _observed(entry, "save"), _observed(entry, "restore")
    if saved is None or restored is None:
        return "unknown"
    result = _bytes(saved - restored, signed=True)
    return result + " (miss → 0 B)" if entry["restore_status"] == "miss" else result


def _comparison_rows(timings: dict, baseline: dict | None) -> list[tuple[dict | None, dict | None]]:
    def identity(entry):
        return entry.get("job", ""), entry["identity"]

    main = (baseline or {}).get("cache_data", {}).get("entries", [])
    main_map = {identity(e): e for e in main}
    current = timings["cache_data"].get("entries", [])
    current_ids = {identity(e) for e in current}
    return ([(e, main_map.get(identity(e))) for e in current]
            + [(None, e) for e in main if identity(e) not in current_ids])


def _totals(timings: dict) -> tuple[int, int, int]:
    restored = saved = unknown = 0
    for entry in timings["cache_data"].get("entries", []):
        if entry["restore_status"] in ("exact", "fallback"):
            if entry["restored_bytes"] is None:
                unknown += 1
            else:
                restored += entry["restored_bytes"]
        if entry["save_status"] == "saved":
            if entry["saved_bytes"] is None:
                unknown += 1
            else:
                saved += entry["saved_bytes"]
    return restored, saved, unknown


def _summary(timings: dict, baseline: dict | None) -> str:
    restored, saved, _ = _totals(timings)
    result = f"Observed compressed archives: restored {_bytes(restored)}; saved {_bytes(saved)} (known sizes only)."
    if "cache_data" not in (baseline or {}):
        result += " No cache-aware main baseline."
    return result


def _notes(timings: dict, baseline: dict | None) -> list[str]:
    data = timings["cache_data"]
    notes = [_summary(timings, baseline),
             f"Job logs collected: {data.get('jobs_collected', 0)}. "
             f"Unknown archive sizes: {_totals(timings)[2]} successful transfer(s)."]
    if data.get("unavailable_jobs"):
        notes.append("Unavailable job logs: " + ", ".join(data["unavailable_jobs"]))
    baseline_data = (baseline or {}).get("cache_data")
    if baseline_data is None:
        notes.append("No cache-aware main baseline; comparisons are unknown, not zero.")
    elif baseline_data.get("unavailable_jobs"):
        notes.append("Unavailable main job logs: " + ", ".join(baseline_data["unavailable_jobs"]))
    notes.append(
        "Per-cache restored/saved deltas compare observed bytes against main. "
        "Missing invocations or measurements are unknown, not zero. "
        "Archive Δ is saved compressed archive bytes minus restored compressed archive bytes, "
        "not per-file growth. A clean miss explicitly uses 0 B restored; exact-hit skips write no archive."
    )
    return notes


def _cells(current: dict | None, main: dict | None) -> list[str]:
    entry = cast(dict, current if current is not None else main)
    return [entry.get("job", ""), entry["identity"],
            _transfer(current, "restore"), _delta(current, main, "restore"),
            _transfer(current, "save"), _delta(current, main, "save"), _archive_delta(current)]


def _headers() -> tuple[str, ...]:
    return ("Job", "Cache invocation", "Restored", "Restored Δ vs main", "Saved",
            "Saved Δ vs main", "Archive Δ (saved − restored)")


def _md(value: str) -> str:
    text = escape(str(value)).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_\[\]|])", r"\\\1", text)


def _markdown(timings: dict, baseline: dict | None, limit: int | None = None, *, standalone: bool = True) -> str:
    rows = _comparison_rows(timings, baseline)
    displayed = rows if limit is None else rows[:limit]
    notes = _notes(timings, baseline)
    lines = ["### CI caches", "", _md(notes[0])] if standalone else []
    lines += [*[_md(note) for note in notes[1:]], "",
              "| " + " | ".join(_headers()) + " |", "| " + " | ".join(["---"] * len(_headers())) + " |"]
    for current, main in displayed:
        lines.append("| " + " | ".join(_md(cell) for cell in _cells(current, main)) + " |")
    if limit is not None and len(rows) > limit:
        lines += ["", f"Showing {limit} of {len(rows)} caches; see the full HTML report for all invocations and keys."]
    for current, main in displayed:
        entry = cast(dict, current if current is not None else main)
        for warning in entry["warnings"]:
            lines.append(f"- {_md(entry.get('job', ''))} / {_md(entry['identity'])}: {_md(warning)}")
    return "\n".join(lines)


def render_cache_summary(timings: dict, baseline: dict | None) -> str:
    """Full Markdown per-cache table for the Actions summary."""
    return _markdown(timings, baseline) if "cache_data" in timings else ""


def render_cache_html(timings: dict, baseline: dict | None) -> str:
    """An embeddable HTML section with every invocation and complete cache keys."""
    if "cache_data" not in timings:
        return ""
    parts = ['<section id="ci-caches"><h2>CI caches</h2>']
    parts += [f"<p>{escape(note)}</p>" for note in _notes(timings, baseline)]
    parts += ["<table><thead><tr>" + "".join(f"<th>{escape(h)}</th>" for h in _headers())
              + "</tr></thead><tbody>"]
    for current, main in _comparison_rows(timings, baseline):
        parts.append("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in _cells(current, main)) + "</tr>")
    parts.append("</tbody></table>")
    for current, main in _comparison_rows(timings, baseline):
        entry = cast(dict, current if current is not None else main)
        label = f"{entry.get('job', '')} / {entry['identity']}"
        parts.append(f"<details><summary>{escape(label)}</summary>")
        for version, record in (("Current", current), ("Main", main)):
            parts.append(f"<h4>{version}</h4>")
            if record is None:
                parts.append("<p>not observed</p>")
                continue
            parts.append("<dl>")
            for title, field in (("Requested key", "requested_key"), ("Matched key", "matched_key"),
                                 ("Saved key", "saved_key")):
                parts.append(f"<dt>{title}</dt><dd><code>{escape(record.get(field) or 'unknown')}</code></dd>")
            parts.append("</dl>")
            parts += [f"<p>{escape(warning)}</p>" for warning in record["warnings"]]
        parts.append("</details>")
    return "\n".join([*parts, "</section>"])


def cache_review_status(
    timings: dict, baseline: dict | None, report_url: str | None = None,
) -> list[dict]:
    """An informational cache section for the existing unified CI review comment."""
    if "cache_data" not in timings:
        return []
    result = {"kind": "info", "title": "CI caches", "summary": _summary(timings, baseline),
              "detail": _markdown(timings, baseline, limit=20, standalone=False)}
    if report_url:
        result["link"] = report_url
        result["link_label"] = "View report"
    return [{"source": "ci caches", "results": [result]}]
