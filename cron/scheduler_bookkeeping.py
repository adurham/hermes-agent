"""Terminal bookkeeping phases shared by scheduler execution paths."""

EMPTY_RESPONSE_ERROR = "Agent completed but produced empty response (model error, timeout, or misconfiguration)"


def fail_empty_response(d, final_response):
    """A "successful" run with a blank answer is a soft failure (never ``ok``): the ordinary tail and
    receipt recovery both book it, so a recovered whitespace answer cannot turn the job green."""
    if d.success and not str(final_response or "").strip():
        d.success = False
        d.error = EMPTY_RESPONSE_ERROR


def finish_interrupted_run(job, execution_id, delivery_error):
    """Shutdown already advanced this run; update a failed notice without spending another fire."""
    from cron import scheduler
    logger = scheduler.logger
    if delivery_error:
        try:
            from cron.jobs import update_job
            update_job(job['id'], {'last_delivery_error': delivery_error})
        except Exception as exc:
            logger.debug('Failed recording delivery_error for interrupted job %s: %s', job['id'], exc, exc_info=True)
    scheduler.finish_execution(execution_id, success=False,
                               error='Interrupted by gateway shutdown before terminal completion.')
