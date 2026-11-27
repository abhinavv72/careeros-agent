"""Sign-in supervisor: keep the dashboard and scheduler alive without a console."""
import logging
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import HTTPError

from agent_launcher import URL, ensure_server, get_health
from agent_runtime import WorkerBusy, exclusive_lock

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.FileHandler(DATA / 'autostart.log', encoding='utf-8')])


def run():
    delay = 5
    browser_opened = False
    while True:
        try:
            ensure_server()
            if not browser_opened:
                try:
                    webbrowser.open(URL)
                    browser_opened = True
                    logging.info('Dashboard opened in browser: %s', URL)
                except Exception as exc:
                    logging.warning('Could not auto-open browser: %s', exc)
            runner_log_path = DATA / 'continuous_runner.log'
            with runner_log_path.open('a+', encoding='utf-8') as log:
                runner_env = os.environ.copy()
                runner_env['PYTHONUNBUFFERED'] = '1'
                # discovery_runner performs its first scan immediately. The
                # application scheduler does not run the legacy sweep.
                runner_env.pop('JOBPILOT_INITIAL_SCAN', None)
                runner = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'continuous_runner.py')],
                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                    env=runner_env)
                discovery_log = (DATA / 'discovery_runner.log').open('a', encoding='utf-8')
                discovery = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'discovery_runner.py')],
                    cwd=ROOT, stdout=discovery_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                    env=runner_env)
                logging.info('Runner started with pid %s', runner.pid)
                logging.info('Discovery started with pid %s', discovery.pid)
                try:
                    while runner.poll() is None:
                        time.sleep(15)
                        if discovery.poll() is not None:
                            logging.warning('Discovery exited with code %s; restarting', discovery.returncode)
                            discovery = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'discovery_runner.py')],
                                cwd=ROOT, stdout=discovery_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                                env=runner_env)
                        health = get_health()
                        if health is None:
                            ensure_server()
                        elif health.get('restart_required') and not health.get('active'):
                            try:
                                ensure_server()
                            except RuntimeError as exc:
                                # A dashboard action can start after the health
                                # check but before the restart lock is acquired.
                                # Defer the update without killing the live runner.
                                if 'Stop the active application run' in str(exc):
                                    logging.info('Backend update deferred; an application run started concurrently')
                                    continue
                                raise
                            except HTTPError as exc:
                                if exc.code == 409:
                                    logging.info('Backend update deferred; an application run started concurrently')
                                    continue
                                raise
                            runner.terminate()
                            try:
                                runner.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                runner.kill()
                                runner.wait(timeout=5)
                            break
                finally:
                    if discovery.poll() is None:
                        discovery.terminate()
                        try: discovery.wait(timeout=5)
                        except subprocess.TimeoutExpired: discovery.kill()
                    discovery_log.close()
                    if runner.poll() is None:
                        try:
                            runner.terminate()
                            runner.wait(timeout=5)
                        except Exception:
                            try:
                                runner.kill()
                            except Exception:
                                pass
                logging.warning('Runner exited with code %s', runner.returncode)
            delay = 5
        except Exception:
            logging.exception('Startup or supervision failed; retrying')
        time.sleep(delay)
        delay = min(delay * 2, 300)


if __name__ == '__main__':
    try:
        with exclusive_lock(DATA / 'autostart.lock'):
            run()
    except WorkerBusy:
        logging.info('JobPilot is already supervised')
