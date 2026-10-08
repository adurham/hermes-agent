"""The canonical delivery queue row is the cron run's send: it obeys the same fire-claim fence,
receipt and settlement rules as a direct send."""
from cron import delivery_queue, executions, jobs, scheduler


def test_claim_stolen_after_the_ownership_sample_queues_no_ghost_send(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "run_job", lambda job, **kw: (True, "raw", "the result", None))
    job = jobs.create_job(prompt="p", schedule="every 1h", deliver="telegram:fixture")
    fire = jobs.claim_job_for_fire(job["id"], return_job=True, force=True)
    samples = []
    real_lost = scheduler._FireOwnership.lost

    def stolen_after_second_sample(self):
        verdict = real_lost(self)
        samples.append(verdict)
        if len(samples) == 2:  # _save_compose_deliver's pre-send sample: a replacement wins now
            jobs.update_job(job["id"], {"fire_claim": dict(fire["fire_claim"], by="replacement")})
        return verdict

    monkeypatch.setattr(scheduler._FireOwnership, "lost", stolen_after_second_sample)
    scheduler.run_one_job(fire)
    execution = executions.latest_execution(job["id"])
    assert samples[:2] == [False, False]
    assert delivery_queue.get_status(execution["id"]) is None
    assert execution["status"] == "failed"
