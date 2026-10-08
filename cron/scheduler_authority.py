"""Transport-only cron execution; output/delivery remain with the firing scheduler."""
import asyncio
import hashlib
import json
import uuid


class CronExecutionUnknown(RuntimeError):
    """The owner may have accepted this fire; never book it as failed or re-fire."""


def journal_path(job_id, request_id):
    from hermes_constants import get_hermes_home
    key = hashlib.sha256(json.dumps([job_id, request_id]).encode()).hexdigest()
    return get_hermes_home() / 'cron' / 'admissions' / (key + '.json')


def run_canonical_job(job, *, extra_prompt=None, cancel_event=None, execution_id=None):
    from gateway.session_cron import owner_for_home, operation
    from hermes_cli.gateway_client import GatewayClientError, connect_gateway
    from hermes_constants import get_hermes_home
    from hermes_state_runtime import RuntimeStoreError
    from utils import atomic_json_write

    params = {'job_id': job['id'], 'request_id': execution_id or job.get('execution_id') or uuid.uuid4().hex,
              'extra_prompt': extra_prompt}
    root = get_hermes_home() / 'cron' / 'admissions'
    journal = journal_path(params['job_id'], params['request_id'])
    record = {'params': params, 'receipt': None}
    if journal.exists():
        record = json.loads(journal.read_text(encoding='utf-8-sig'))
        if record['params'] != params:
            raise CronExecutionUnknown('cron admission identity conflict')
    attempted = journal.exists()
    owner = owner_for_home(get_hermes_home())

    def save():
        root.mkdir(parents=True, exist_ok=True)
        # The journal is the only evidence a lost reply was admitted: survive power loss.
        atomic_json_write(journal, record, mode=0o600, fsync_dir=True)

    async def refused(call, exc):
        # A lost reply may still have been admitted. Only the owner's own verdict (a bounded
        # reason code, never a disconnect/timeout) that its ledger holds no admission for this
        # fire is a refusal: book it as an ordinary failed run, never unknown.
        verdict = isinstance(exc, RuntimeStoreError) or (
            isinstance(exc, GatewayClientError) and str(exc).replace('_', '').isalnum())
        return verdict and (await call('recover', params))['status'] == 'missing'

    async def observe(call):
        nonlocal attempted
        attempted = True
        save()
        try:
            receipt = await call('submit', params)
        except (RuntimeStoreError, GatewayClientError) as exc:
            if await refused(call, exc):
                journal.unlink(missing_ok=True)
                attempted = False
            raise
        record['receipt'] = receipt
        save()
        # Cron turns run for minutes: back the status read off to 2 s instead of 10 reads/s.
        delay = .1
        while True:
            if cancel_event is not None and cancel_event.is_set():
                await call('cancel', receipt)
            state = await call('status', receipt)
            if state['status'] == 'terminal':
                job.update(state.get('job_flags') or {})
                return tuple(state['result'])
            if state['status'] == 'unknown':
                raise CronExecutionUnknown('unknown_execution: cron admission was not replayed')
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 2.0)

    async def remote():
        async with connect_gateway() as client:
            return await observe(lambda op, data: client.rpc('cron.' + op, **data))

    try:
        if owner is not None:
            authority, loop = owner
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                raise RuntimeError('cron synchronous execution must run off the owner event loop')
            return asyncio.run_coroutine_threadsafe(
                observe(lambda op, data: operation(authority, op, data)), loop).result()
        return asyncio.run(remote())
    except Exception as exc:
        if attempted:
            raise CronExecutionUnknown(f'Cron execution unverified; reconcile {journal}: {exc}') from exc
        error = f'{type(exc).__name__}: {exc}'
        return False, f'# Cron Job: {job["id"]} (FAILED)\n\n{error}\n', '', error

def reconcile_pending(*, allow_connect=True):
    """Observe prepared fires; never submit missing or interrupted work.

    With allow_connect=False, a headless tick leaves durable receipts for the next
    live owner instead of ensuring or spawning a gateway.
    """
    from gateway.session_cron import owner_for_home, operation
    from hermes_constants import get_hermes_home
    from hermes_cli.gateway_client import connect_gateway
    from cron.jobs import pause_job, mark_job_run, save_job_output
    from cron.scheduler import _compose_run_delivery, _is_cron_silence_response
    from cron.delivery_queue import enqueue
    from cron.executions import finish_execution
    import logging

    root = get_hermes_home() / 'cron' / 'admissions'
    for journal in sorted(root.glob('*.json')):
        try:
            record = json.loads(journal.read_text(encoding='utf-8-sig'))
            params = record['params']
            if journal != journal_path(params['job_id'], params['request_id']):
                raise ValueError('cron journal identity conflict')
            owner = owner_for_home(get_hermes_home())
            if owner is None and not allow_connect:
                continue
            async def observe():
                if owner is not None:
                    return await operation(owner[0], 'recover', params)
                async with connect_gateway() as client:
                    return await client.rpc('cron.recover', **params)
            if owner is not None:
                state = asyncio.run_coroutine_threadsafe(observe(), owner[1]).result(timeout=20)
            else:
                state = asyncio.run(observe())
            if state['status'] != 'terminal':
                if state['status'] in {'unknown', 'missing'}:
                    pause_job(params['job_id'], reason='Canonical cron ' + state['status'] + '; no automatic re-execution')
                continue
            success, output, answer, error = state['result']
            job = state['job']
            job['execution_id'] = params['request_id']
            output_file = save_job_output(job['id'], output)
            content, _, silent, _, _ = _compose_run_delivery(
                job, success=success, error=error, final_response=answer, output_file=output_file)
            deliver = bool(content.strip()) and not silent and not (success and _is_cron_silence_response(content))
            if deliver:
                enqueue(params['request_id'], job, content, for_failure=not success)
            mark_job_run(job['id'], success, error, status='delivery_queued' if deliver else None,
                         execution_id=params['request_id'])
            # The journal is the only link from the firer's ledger row to this receipt: settle
            # the row (when its firer exited before its own bookkeeping) before deleting it.
            from cron.delivery_outcome import settle_quietly, settled_outcome
            finish_execution(params['request_id'], success=success, error=error, departed_owner=True,
                             delivery_outcome=(settled_outcome(params['request_id']) or 'queued')
                             if deliver else 'suppressed')
            if deliver:
                settle_quietly(job['id'], params['request_id'])
            journal.unlink(missing_ok=True)
        except Exception:
            logging.getLogger(__name__).warning('Cron receipt recovery deferred: %s', journal, exc_info=True)
