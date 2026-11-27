"""Bounded, single-instance scheduler for the local JobPilot API."""
import json
import logging
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent_runtime import WorkerBusy, exclusive_lock

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
SCHEDULE_STATE = DATA / 'scheduler-state.json'
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(DATA / 'continuous_runner.log', encoding='utf-8'), logging.StreamHandler()])
API = 'http://127.0.0.1:8765/api/'
_last_empty_preview = None
_last_empty_preview_at = 0.0
HEADERS = {'X-Jobpilot-Request': 'local-dashboard', 'X-Jobpilot-Autonomous': 'true',
           'Content-Type': 'application/json'}


def api(path, payload=None):
    request = Request(API + path, data=json.dumps(payload).encode() if payload is not None else None,
                      headers=HEADERS, method='POST' if payload is not None else 'GET')
    # The backend can wait up to 60 seconds for a SQLite write lock. A shorter
    # client timeout makes a valid request look failed while it is still running.
    with urlopen(request, timeout=90) as response:
        return json.load(response)


def wait_idle():
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = api('state')
        if state.get('autonomy_paused'):
            return False
        if not state.get('active'):
            return True
        time.sleep(3)
    raise RuntimeError('Worker still active after waiting two minutes')


def action(name, limit=50, job_ids=None):
    # A different dashboard user can start a task between our idle poll and
    # POST. Back off on that normal race instead of treating it as a failure.
    for attempt in range(6):
        if not wait_idle():
            return False
        payload = {'action': name, 'limit': limit, 'dry_run': False}
        if job_ids:
            payload['job_ids'] = job_ids
        try:
            result = api('runs', payload)
            break
        except HTTPError as exc:
            database_busy = exc.code == 503 and exc.headers is not None and exc.headers.get('Retry-After') == '3'
            if (exc.code != 409 and not database_busy) or attempt == 5:
                raise
            if database_busy:
                logging.warning('%s dispatch waiting for SQLite (attempt %s of 6)', name, attempt + 1)
            time.sleep(min(3 * (attempt + 1), 15))
    run_id = result.get('id')
    logging.info('%s started: %s', name, run_id)
    poll_start = time.monotonic()
    while True:
        state = api('state')
        finished = next((run for run in state.get('runs', []) if run.get('id') == run_id), None)
        if finished and finished.get('status') not in ('Running','StopRequested'):
            break
        if not state.get('active') and time.monotonic() - poll_start > 30:
            logging.warning('Supervisor has cleared active status for %s; proceeding.', run_id)
            break
        if time.monotonic() - poll_start > 1200:
            logging.warning('Action %s timed out after 20 minutes; breaking wait loop.', run_id)
            break
        if state.get('autonomy_paused'):
            return False
        time.sleep(3)
    if not finished or finished.get('status') != 'Completed':
        if name == 'apply' and finished and finished.get('status') in ('Completed', 'Failed'):
            logging.info('Apply run finished with status %s; continuing autonomous processing without halting.', finished.get('status'))
        else:
            raise RuntimeError(f'{name} ended without a successful completion: {finished}')
    if name in ('discover', 'naukri', 'remotive', 'himalayas', 'portals'):
        try:
            state = json.loads(SCHEDULE_STATE.read_text(encoding='utf-8')) if SCHEDULE_STATE.exists() else {}
        except (OSError, ValueError):
            state = {}
        state[name] = time.time()
        temporary = SCHEDULE_STATE.with_suffix('.tmp')
        temporary.write_text(json.dumps(state), encoding='utf-8')
        temporary.replace(SCHEDULE_STATE)
    return True


def apply_ready_batch():
    """Use the saved bulk size while retaining one submission worker."""
    try:
        settings = json.loads((ROOT / 'config' / 'settings.json').read_text(encoding='utf-8'))
        limit = max(1, min(100, int(settings.get('bulkBatchSize', 25))))
    except (OSError, ValueError, TypeError):
        limit = 25
    global _last_empty_preview, _last_empty_preview_at
    state = api('state')
    if state.get('autonomy_paused') or state.get('active'):
        return False
    preview = api(f'apply/preview?limit={limit}')
    ids = [item['job_id'] for item in preview.get('items', [])][:limit]
    if ids:
        _last_empty_preview = None
        logging.info('Application preview scanned=%s ready=%s blocked=%s',
                     preview.get('scanned'), len(ids), preview.get('blocked', {}))
        return action('apply', limit=len(ids), job_ids=ids)
    summary = (preview.get('scanned'), preview.get('blocked', {}), preview.get('scan_limit_reached', False))
    checked_at = time.monotonic()
    if summary != _last_empty_preview or checked_at - _last_empty_preview_at >= 300:
        logging.info('Application preview scanned=%s ready=0 blocked=%s scan_limit_reached=%s', *summary)
        _last_empty_preview, _last_empty_preview_at = summary, checked_at
    return False


def startup_discovery_sweep():
    """Do a fresh search at user sign-in even when regular source cadence is not due."""
    import os
    if os.environ.pop('JOBPILOT_INITIAL_SCAN', None) != '1':
        return
    logging.info('Starting the sign-in discovery sweep')
    try:
        apply_ready_batch()
    except Exception as exc:
        logging.warning('Startup queue processing failed: %s', exc)
    try:
        # The discover worker covers configured employer boards, portal leads,
        # remote boards, scoring, queueing, and its small validated apply batch.
        action('discover', limit=5)
    except Exception as exc:
        logging.warning('Sign-in employer and remote discovery failed: %s', exc)
    try:
        action('naukri', limit=1)
        action('score', limit=100)
        action('queue', limit=50)
    except Exception as exc:
        logging.warning('Sign-in Naukri discovery or queue refresh failed: %s', exc)
    logging.info('Sign-in discovery sweep finished')


def run_loop():
    """Submit ready jobs while discovery runs in its own supervised process."""
    logging.info('Independent application scheduler started')
    retry_due = 0.0
    while True:
        try:
            if api('state').get('autonomy_paused'):
                time.sleep(10)
                continue
            # Ready applications have priority. A retry write can wait on a
            # discovery transaction, so it must never stall the apply path.
            applied = apply_ready_batch()
            if time.monotonic() >= retry_due:
                retry_due = time.monotonic() + 180
                try:
                    action('retry', limit=50)
                except (HTTPError, URLError, OSError, ValueError, RuntimeError) as exc:
                    logging.warning('Retry maintenance failed; applications remain scheduled: %s', exc)
            if not applied:
                time.sleep(10)
        except (HTTPError, URLError, OSError, ValueError, RuntimeError) as exc:
            logging.warning('Application scheduler retry: %s', exc)
            time.sleep(10)


def legacy_run_loop():
    due = {'discover': 0.0, 'naukri': 0.0, 'remotive': 0.0, 'himalayas': 0.0, 'portals': 0.0, 'retry': 0.0, 'score': 0.0}
    intervals = {'discover': 600, 'naukri': 1800, 'remotive': 3600, 'himalayas': 3600,
                 'portals': 3600,
                 'retry': 180, 'score': 300}
    failures = {name: 0 for name in due}
    try:
        saved = json.loads(SCHEDULE_STATE.read_text(encoding='utf-8'))
        for name in ('discover', 'naukri', 'remotive', 'himalayas', 'portals'):
            elapsed = time.time() - float(saved.get(name, 0))
            if 0 <= elapsed < intervals[name]:
                due[name] = time.monotonic() + intervals[name] - elapsed
    except (OSError, ValueError, TypeError):
        pass
    # Only successful completions advance a source cadence. A recent Failed or
    # Interrupted discovery must not postpone the next retry just because its
    # started_at timestamp is recent; this matters after laptop sleep/restarts.
    logging.info('Autonomous scheduler started')
    startup_discovery_sweep()
    while True:
        try:
            state = api('state')
            if state.get('autonomy_paused'):
                time.sleep(15)
                continue
            if not wait_idle():
                continue
            current = time.monotonic()
            for name in ('discover', 'naukri', 'remotive', 'himalayas', 'portals', 'retry', 'score'):
                if current < due[name]:
                    continue
                try:
                    if not action(name, limit=5 if name == 'discover' else 50):
                        break
                    failures[name] = 0
                    due[name] = time.monotonic() + intervals[name]
                    if name != 'score':
                        action('score', limit=100)
                    action('queue', limit=50)
                    if name in ('discover','naukri','remotive','himalayas','portals'):
                        apply_ready_batch()
                except (HTTPError, URLError, OSError, ValueError, RuntimeError) as exc:
                    failures[name] += 1
                    due[name] = time.monotonic() + min(intervals[name], 60 * 2 ** min(failures[name], 6))
                    logging.warning('%s failed; other sources continue: %s', name, exc)
            if api('state').get('autonomy_paused'):
                continue
            if not apply_ready_batch():
                time.sleep(30)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            logging.warning('API unavailable: %s', exc)
            time.sleep(15)
        except Exception:
            logging.exception('Unexpected runner error')
            time.sleep(30)


if __name__ == '__main__':
    try:
        with exclusive_lock(DATA / 'continuous-runner.lock'):
            run_loop()
    except WorkerBusy:
        logging.info('Another scheduler owns the single-instance lock')
