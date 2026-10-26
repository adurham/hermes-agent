"""Each update pipeline stage mark names the next stage to a watching Desktop hand-off window."""
from __future__ import annotations

import json


def test_stage_marks_publish_the_next_stage_to_the_handoff_window(tmp_path, monkeypatch):
    from hermes_cli import update_receipt

    status = tmp_path / "hermes-update-status.1"
    monkeypatch.setenv("HERMES_UPDATE_STATUS_FILE", str(status))

    def shown() -> str:
        return json.loads(status.read_text(encoding="utf-8"))["message"] if status.exists() else ""

    update_receipt.record_stage("snapshot", "failed")  # a failed backup does not stop the update
    assert shown() == "Downloading updates"
    update_receipt.record_stage("apply", "success")
    assert shown() == "Updating dependencies"
    update_receipt.record_stage("deps", "failed")  # the update ends here: nothing new to announce
    assert shown() == "Updating dependencies"
