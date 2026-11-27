"""Independent source scheduler; the API application worker stays available."""
import argparse
import logging
import sys
import time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(line_buffering=True)

import jobpilot
from agent_runtime import WorkerBusy, exclusive_lock

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.FileHandler(jobpilot.DATA / 'discovery_runner.log', encoding='utf-8')])


CODE_FILES = ('discovery_runner.py', 'agent_worker.py', 'jobpilot.py', 'answer_engine.py',
              'candidate_kb.py', 'portal_discovery.py', 'remote_discovery.py', 'himalayas_discovery.py')


def code_stamp():
    """Modification times of the code this process imported."""
    stamps = []
    for name in CODE_FILES:
        try:
            stamps.append((jobpilot.ROOT / name).stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    return tuple(stamps)


def run():
    import agent_worker
    started_with = code_stamp()
    # Naukri direct Apply is the channel that produces confirmed applications;
    # keep employer boards fast and bounded so scoring/queueing stays fresh 24/7.
    sources = (('naukri', 120, 5), ('discover', 900, 10),
               ('remotive', 3600, 50), ('himalayas', 3600, 50), ('portals', 3600, 50))
    due = {name: 0 for name, _, _ in sources}
    failures = {name: 0 for name, _, _ in sources}
    while True:
        if code_stamp() != started_with:
            # autostart relaunches this process, so new discovery code takes effect
            # without restarting the API or interrupting an application.
            logging.info('Discovery code changed; exiting so the supervisor restarts it')
            return
        if (jobpilot.DATA / 'autonomy-paused').exists() or (jobpilot.DATA / 'discovery-paused').exists():
            time.sleep(10)
            continue
        for name, interval, limit in sources:
            if code_stamp() != started_with:
                logging.info('Discovery code changed; exiting so the supervisor restarts it')
                return
            if time.monotonic() < due[name]:
                continue
            started = time.monotonic()
            try:
                jobpilot.wait_for_application_write_window()
                logging.info('%s discovery started', name)
                if name == 'discover':
                    # Run only employer ATS boards here; remotive/himalayas/portals
                    # already have their own cadences in `sources`.
                    result = jobpilot.cmd_discover(argparse.Namespace(limit=limit, on_batch=None))
                else:
                    result = agent_worker.run(argparse.Namespace(action=name, limit=limit, job=None,
                        jobs=None, dry_run=False, url=None))
                jobpilot.wait_for_application_write_window()
                jobpilot.cmd_score(argparse.Namespace(limit=5000))
                jobpilot.wait_for_application_write_window()
                jobpilot.cmd_queue(argparse.Namespace(limit=100))
                result = result or {}
                logging.info('%s discovery results=%s duration=%.1fs', name, result, time.monotonic() - started)
                if result.get('errors') and not result.get('succeeded'):
                    raise RuntimeError('All source searches failed')
                failures[name] = 0
                effective_interval = 600 if (name == 'naukri' and jobpilot.naukri_quota_active()) else interval
                due[name] = time.monotonic() + effective_interval
                logging.info('%s discovery completed next_due_in=%ss', name, effective_interval)
            except Exception:
                failures[name] += 1
                retry_delay = min(max(interval, 600), 60 * 2 ** min(failures[name], 6))
                due[name] = time.monotonic() + retry_delay
                logging.exception('%s discovery failed retry_in=%ss', name, retry_delay)
        time.sleep(10)


if __name__ == '__main__':
    import os
    os.environ['JOBPILOT_DISCOVERY_ONLY'] = '1'
    try:
        with exclusive_lock(jobpilot.DATA / 'discovery-worker.lock'):
            run()
    except WorkerBusy:
        logging.info('Another discovery scheduler is running')
