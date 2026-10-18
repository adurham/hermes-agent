"""The cron store resolves the profile home at call time, never at import.

One process serves many profiles, so a home captured when ``cron.jobs`` was
imported is the launch profile's: anything derived from it would read or write
that profile's store on behalf of whichever profile is active now.
"""


def test_partial_repoint_keeps_unset_paths_on_active_profile(tmp_path, monkeypatch):
    """Re-pointing only CRON_DIR must not send the jobs file to the import-time home."""
    from cron import jobs

    home = tmp_path / "active-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "pinned-cron")

    store = jobs._current_cron_store()

    assert store.cron_dir == tmp_path / "pinned-cron"
    assert store.jobs_file == home.resolve() / "cron" / "jobs.json"
    assert store.output_dir == home.resolve() / "cron" / "output"


def test_cron_doctor_scripts_dir_follows_active_profile(tmp_path, monkeypatch):
    """`hermes cron` script checks resolve against the active profile's scripts dir."""
    from hermes_cli.cron import _script_health_issue

    home = tmp_path / "active-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    script = home / "scripts" / "collect.sh"
    script.parent.mkdir(parents=True)
    script.write_text("echo ok\n", encoding="utf-8")

    assert _script_health_issue("collect.sh") is None
