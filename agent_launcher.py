"""Start the local dashboard, verify readiness, then open the browser."""
import json
import importlib.util
import os
from pathlib import Path
import site
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import webbrowser

ROOT=Path(__file__).resolve().parent
URL='http://127.0.0.1:8765'


def child_environment():
    """Expose packages installed by `python -m pip --user` to the detached server."""
    env=os.environ.copy()
    user_site=site.getusersitepackages()
    if isinstance(user_site,str) and user_site and Path(user_site).is_dir():
        paths=[part for part in env.get('PYTHONPATH','').split(os.pathsep) if part]
        if user_site not in paths:
            env['PYTHONPATH']=os.pathsep.join([*paths,user_site])
    return env


def get_health():
    try:
        with urlopen(URL+'/api/health',timeout=2) as response:return json.load(response)
    except HTTPError as exc:
        raise RuntimeError('An older or different server is running on port 8765. Close its launcher and try again.') from exc
    except (URLError,TimeoutError,OSError):return None


def check_server_dependencies():
    missing=[name for name in ('fastapi','uvicorn') if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError('Dashboard dependencies are missing ('+', '.join(missing)+'). Install them with: python -m pip install -r requirements-agent.txt')


def ensure_server():
    from agent_runtime import exclusive_lock, WorkerBusy
    with exclusive_lock(ROOT/'data'/'dashboard-launcher.lock'):
        health=get_health()
        if health:
            if health.get('service')!='jobpilot' or Path(health.get('root','')).resolve()!=ROOT.resolve():
                raise RuntimeError('Port 8765 belongs to a different service or JobPilot workspace.')
            if not health.get('restart_required'):return health
            if health.get('active'):raise RuntimeError('Stop the active application run before restarting the updated backend.')
            check_server_dependencies()
            request=Request(URL+'/api/shutdown',data=b'{}',method='POST',headers={'Content-Type':'application/json','X-Jobpilot-Request':'local-dashboard'})
            with urlopen(request,timeout=3) as response:json.load(response)
            for _ in range(50):
                if get_health() is None:break
                time.sleep(0.1)
            else:raise RuntimeError('The existing backend has not stopped. Its worker was not interrupted.')
            # HTTP can stop responding before the old process releases its
            # lifetime lock. Wait for teardown before spawning a replacement.
            for _ in range(100):
                try:
                    with exclusive_lock(ROOT/'data'/'dashboard-server.lock'):
                        break
                except WorkerBusy:
                    time.sleep(0.1)
            else:
                raise RuntimeError('The previous dashboard is still shutting down; startup will retry.')
        else:
            check_server_dependencies()
        logs=ROOT/'data'/'server';logs.mkdir(parents=True,exist_ok=True)
        path=logs/'dashboard.log'
        with path.open('a',encoding='utf-8') as log:
            process=subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'agent_server.py')],cwd=ROOT,
                stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                env=child_environment(),
                creationflags=(subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0)
        for _ in range(100):
            health=get_health()
            if health:
                if health.get('service')!='jobpilot' or Path(health.get('root','')).resolve()!=ROOT.resolve():
                    raise RuntimeError('Port 8765 belongs to a different service or JobPilot workspace.')
                if health.get('ready'):
                    # Another launcher may have won the port race after our
                    # initial check. Reuse that healthy local server.
                    return health
            if process.poll() is not None and not health:
                raise RuntimeError(f'Dashboard failed to start. Read {path}')
            time.sleep(0.1)
        raise RuntimeError(f'Dashboard did not become ready. Read {path}')


if __name__=='__main__':
    try:
        ensure_server()
        webbrowser.open(URL)
        print('JobPilot is running. Closing this launcher does not stop the dashboard.')
    except Exception as exc:
        print(f'JobPilot could not start: {exc}',file=sys.stderr)
        raise SystemExit(1)
