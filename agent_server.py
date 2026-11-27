"""Loopback-only dashboard/API and single-worker supervisor."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import threading
import uuid
import hashlib
from dataclasses import asdict
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import jobpilot
from agent_runtime import exclusive_lock, recover_attempts, WorkerBusy
from application_support import atomic_json, recovery_guidance, retry_policy
from ats_forms import adapter_capabilities, supports_autosubmit

ROOT=Path(__file__).resolve().parent
def backend_build():
    digest=hashlib.sha256()
    for name in ('agent_server.py','agent_worker.py','jobpilot.py','ats_forms.py','profile_agent.py','agent_runtime.py','application_support.py','portal_discovery.py','remote_discovery.py','himalayas_discovery.py','ats_detector.py','continuous_runner.py','autostart.py','email_diary_service.py','matching/engine.py','matching/models.py','matching/requirements.py'):
        digest.update((ROOT/name).read_bytes())
    return digest.hexdigest()


LOADED_BUILD=backend_build()
_server=None
@asynccontextmanager
async def lifespan(app):
    initialize()
    yield


app=FastAPI(title='Local Job Agent', docs_url=None, redoc_url=None, lifespan=lifespan)
_guard=threading.Lock()
_active=None
_process=None
_receipt_audit_cache={'at':0.0,'value':{'applied_without_receipt':0,'job_ids':[]}}
def pause_file():
    # Resolved per call so tests that patch jobpilot.DATA never pause the real agent.
    return jobpilot.DATA/'autonomy-paused'


def external_worker_is_busy():
    """Detect CLI batches that are outside this server's in-memory run state."""
    try:
        with exclusive_lock(jobpilot.DATA/'agent-worker.lock'):
            return False
    except WorkerBusy:
        return True


def initialize():
    try:
        # Hold the same process lock as the worker while recovering run rows.
        # A second server that loses the port race must not mark the live
        # worker Interrupted or contend for its SQLite write transaction.
        with exclusive_lock(jobpilot.DATA/'agent-worker.lock'):
            import email_diary_service
            email_diary_service.init_diary_db()
            db=jobpilot.connect()
            try:
                db.execute('''CREATE TABLE IF NOT EXISTS agent_runs(id TEXT PRIMARY KEY,action TEXT,status TEXT,
                    started_at TEXT,completed_at TEXT,exit_code INTEGER,log_path TEXT)''')
                db.execute("UPDATE agent_runs SET status='Interrupted',completed_at=? WHERE status IN ('Running','StopRequested')",(jobpilot.now(),))
                # Reconcile queue snapshots left behind when an older scorer changed only
                # the tracker status. Preserve attempt rows and any submission checkpoints.
                db.execute("UPDATE jobs SET status='ManualReview' WHERE status='Queued' AND EXISTS(SELECT 1 FROM application_attempts a WHERE a.job_id=jobs.id AND a.step='submit' AND a.status='ManualReview' AND a.completed_at IS NULL)")
                db.execute("UPDATE application_queue SET status='Applied',error=NULL,next_attempt_at=NULL WHERE status='Queued' AND EXISTS(SELECT 1 FROM jobs j WHERE j.id=application_queue.job_id AND j.status='Applied')")
                db.execute("UPDATE application_queue SET status='ReviewRequired',error=COALESCE(error,'Application needs review') WHERE status='Queued' AND (EXISTS(SELECT 1 FROM jobs j WHERE j.id=application_queue.job_id AND j.status='ManualReview') OR EXISTS(SELECT 1 FROM application_attempts a WHERE a.job_id=application_queue.job_id AND a.step='submit' AND a.status='Applied'))")
                db.execute("""UPDATE application_queue SET status='Skipped',error=COALESCE(error,'Queue entry reconciled; listing is no longer queued'),next_attempt_at=NULL
                    WHERE status='Queued'
                      AND EXISTS(SELECT 1 FROM jobs j WHERE j.id=application_queue.job_id AND j.status IN ('Qualified','Discovered','Skipped','SkippedBlocked','Rejected'))
                      AND NOT EXISTS(SELECT 1 FROM application_attempts a WHERE a.job_id=application_queue.job_id AND (a.step='submit' OR a.completed_at IS NULL))""")
                db.commit()
            finally:
                db.close()
            recover_idle_attempts()
    except WorkerBusy:
        logging.getLogger(__name__).warning('Skipped startup reconciliation because a JobPilot worker is active.')


def recover_idle_attempts():
    try:
        with exclusive_lock(jobpilot.DATA/'browser-application.lock'):
            db=jobpilot.connect()
            try:jobpilot.recover_application_attempts(db)
            finally:db.close()
    except WorkerBusy:
        pass  # Never reconcile an attempt owned by another live process.


@app.middleware('http')
async def local_only(request:Request, call_next):
    from fastapi.responses import JSONResponse
    host=request.headers.get('host','').split(':')[0]
    if host not in ('127.0.0.1','localhost'):
        return JSONResponse({'detail':'Local access only'},status_code=403)
    origin=request.headers.get('origin')
    if origin:
        try:
            parsed=urlparse(origin)
            local=(parsed.scheme==request.url.scheme and parsed.hostname in ('localhost','127.0.0.1')
                   and (parsed.port or (443 if parsed.scheme=='https' else 80))==(request.url.port or 80)
                   and not parsed.username and not parsed.password and not parsed.path
                   and not parsed.query and not parsed.fragment)
        except ValueError:
            local=False
        if not local:return JSONResponse({'detail':'Foreign origin rejected'},status_code=403)
    if request.method in ('POST','PUT','DELETE','PATCH'):
        if request.headers.get('x-jobpilot-request')!='local-dashboard':
            return JSONResponse({'detail':'Dashboard request header required'},status_code=403)
    return await call_next(request)


def query(sql, parameters=()):
    db=jobpilot.connect()
    try:return [dict(row) for row in db.execute(sql,parameters)]
    finally:db.close()


@app.get('/')
def home(): return FileResponse(ROOT/'dashboard.html',headers={'Cache-Control':'no-store'})


@app.get('/assets/dashboard-product.css')
def dashboard_styles():
    return FileResponse(ROOT/'dashboard-product.css',media_type='text/css',headers={'Cache-Control':'no-store'})


@app.get('/api/health')
def health():
    return {'service':'jobpilot','ready':True,'pid':os.getpid(),'root':str(ROOT),
            'build':LOADED_BUILD,'restart_required':backend_build()!=LOADED_BUILD,'active':_active}


@app.post('/api/shutdown')
def shutdown():
    with _guard:
        if _active:raise HTTPException(409,'Stop the application worker before restarting the dashboard')
        if _server is None:raise HTTPException(409,'Restart through the process that hosts this API')
        _server.should_exit=True
    return {'stopping':True}


@app.get('/api/state')
def state():
    from datetime import time as day_start
    from zoneinfo import ZoneInfo
    local_date=datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Kolkata')).date()
    today_start=datetime.combine(local_date,day_start.min,tzinfo=ZoneInfo('Asia/Kolkata')).astimezone(timezone.utc).isoformat(timespec='seconds')
    period_rows=query("""SELECT j.id,MAX(a.completed_at) AS applied_at FROM jobs j
        LEFT JOIN application_attempts a ON a.job_id=j.id AND a.status='Applied'
        WHERE j.status='Applied' GROUP BY j.id""")
    applied_today=sum(bool(row['applied_at'] and row['applied_at']>=today_start) for row in period_rows)
    applied_before=sum(bool(row['applied_at'] and row['applied_at']<today_start) for row in period_rows)
    applied_undated=sum(not row['applied_at'] for row in period_rows)
    global _receipt_audit_cache
    import time as _time
    if _time.monotonic()-_receipt_audit_cache['at'] > 60:
        applied_rows=query("SELECT id FROM jobs WHERE status='Applied'")
        missing_receipts=[]
        for row in applied_rows:
            job_id=row['id']; evidence=jobpilot.DATA/'screenshots'/f'job-{job_id}'
            if not list(evidence.glob('attempt-*/receipt.json')) and not (jobpilot.DATA/'screenshots'/f'job-{job_id}-receipt.json').is_file(): missing_receipts.append(job_id)
        _receipt_audit_cache={'at':_time.monotonic(),'value':{'applied_without_receipt':len(missing_receipts),'job_ids':missing_receipts}}
    adapters={name:adapter_capabilities(name) for name in ('greenhouse','lever','naukri','remotive','himalayas','google_jobs','wellfound','instahyre','hirist','trueup','offcampus_jobs4u','linkedin','indeed','ashby','bamboohr','smartrecruiters','workday','icims','taleo','oracle','teamtailor','jobvite','workable','generic')}
    summaries={name:('Submit + confirmation validated' if supports_autosubmit(name)
                      else 'Open a Google Jobs search manually' if name=='google_jobs'
                      else 'Manual handoff; site automation disabled' if name=='linkedin'
                      else 'Inspection/fill only; submit not validated') for name,capability in adapters.items()}
    summaries['captcha']='Human verification fallback; local AI experimental'
    recent=query("""SELECT j.id,j.company,j.title,j.status,j.ats,
        (SELECT MAX(a.completed_at) FROM application_attempts a WHERE a.job_id=j.id AND a.status='Applied') AS applied_at
        FROM jobs j WHERE j.status='Applied' ORDER BY applied_at DESC,j.id DESC LIMIT 5""")
    for item in recent:
        folder=jobpilot.DATA/'screenshots'/f"job-{item['id']}"
        item['has_receipt']=any(folder.glob('attempt-*/receipt.json')) or (jobpilot.DATA/'screenshots'/f"job-{item['id']}-receipt.json").is_file()
    pending=query("""SELECT COUNT(*) AS n FROM jobs j WHERE j.status<>'Applied' AND EXISTS
        (SELECT 1 FROM application_attempts a WHERE a.job_id=j.id AND a.step='submit')""")[0]['n']
    active_application=None
    if _active:
        active_runs=query('SELECT action,log_path FROM agent_runs WHERE id=?',(_active,))
        if active_runs and active_runs[0]['action'] in ('apply','custom-link'):
            run=active_runs[0]
            current=query("""SELECT j.id AS job_id,j.company,j.title,j.status,j.apply_url,a.step,a.started_at
                FROM application_attempts a JOIN jobs j ON j.id=a.job_id
                WHERE a.completed_at IS NULL ORDER BY a.id DESC LIMIT 1""")
            if current:
                item=current[0]
                item['phase']='Submission sent; awaiting employer confirmation' if item['step']=='submit' else 'Opening application and checking required fields'
                active_application=item
            else:
                try:log_text=Path(run['log_path']).read_text(encoding='utf-8',errors='replace')[-20000:]
                except OSError:log_text=''
                found=list(re.finditer(r'Opening job (\d+):\s*([^\r\n]+)',log_text))
                if found:
                    job_id=int(found[-1].group(1))
                    rows=query('SELECT id AS job_id,company,title,status,apply_url FROM jobs WHERE id=?',(job_id,))
                    if rows:active_application=rows[0] | {'phase':'Preparing application'}
    return {'counts':query('SELECT status,COUNT(*) count FROM jobs GROUP BY status'),
            'runs':query('SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT 10'),
            'active':_active, 'active_application':active_application, 'autonomy_paused':pause_file().exists(),
            'receipt_audit':_receipt_audit_cache['value'],
            'recent_applications':recent,'pending_confirmation_count':pending,
            'applied_periods':{'today':applied_today,'before_today':applied_before,
                               'unknown_date':applied_undated,'date':local_date.isoformat(),'timezone':'Asia/Kolkata'},
            'criteria':jobpilot.load_json('eligibility.json'),
            'capabilities':summaries,'adapter_capabilities':adapters}


def waiting_challenge_for_active_run():
    if not _active:return False
    try:challenge=json.loads((jobpilot.DATA/'captcha_challenge.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return False
    return challenge.get('run_id')==_active and challenge.get('status')=='WaitingForUser'


def submitted_duplicate_ids(job):
    """Other listings of the same requisition that already reached the submit step."""
    from application_state import submitted_duplicates
    if 'id' not in job.keys() or 'company' not in job.keys():return ()
    db=jobpilot.connect()
    try:return submitted_duplicates(db,job)
    finally:db.close()


def attach_application_states(rows, key='id'):
    """Expose the authoritative state machine result beside legacy status columns."""
    from application_state import states_for
    db=jobpilot.connect()
    try:states=states_for(db,[row[key] for row in rows],jobpilot.DATA)
    finally:db.close()
    for row in rows:
        result=states.get(row[key])
        row['application_state']=result.as_dict() if result else None


def submission_readiness(job, queue, submitted, settings=None, profile=None, rules=None, challenge_waiting=None):
    """Return the single authoritative readiness contract used by list and Apply."""
    settings=settings if settings is not None else jobpilot.load_json('settings.json')
    profile=profile if profile is not None else jobpilot.load_json('profile.json')
    rules=rules if rules is not None else jobpilot.load_json('eligibility.json')
    def blocked(code, message):
        return {'ready':False,'reason_code':code,'message':message}
    if job['status'] != 'Queued': return blocked('job_not_queued','Add this job to the application queue first.')
    if not queue or queue['status'] != 'Queued': return blocked('queue_not_ready','This job is not active in the application queue.')
    ats=str(job['ats'] or '').lower()
    capability=adapter_capabilities(ats)
    if not (capability['tested'] and capability['submit'] and capability['confirm']):
        return blocked('unsupported_adapter',f'{ats or "unknown"} has no validated submit-and-confirm adapter.')
    allowed={str(value).lower() for value in settings.get('allowedAutoSubmitAts',[])}
    if ats not in allowed or not settings.get('unattended') or not settings.get('autoSubmit'):
        return blocked('submission_disabled','Auto-submit is disabled for this adapter or in settings.')
    if submitted: return blocked('already_submitted','A submit checkpoint exists; inspect the attempt before taking another action.')
    duplicates=submitted_duplicate_ids(job)
    if duplicates: return blocked('duplicate_listing','Another listing of this opening was already submitted: job '+', '.join(map(str,duplicates))+'.')
    if _active:
        # Discovery, scoring, and queue maintenance do not use the application
        # browser. Keep their progress visible without falsely marking every
        # queued role as blocked; the apply endpoint still enforces one worker.
        if (challenge_waiting if challenge_waiting is not None else waiting_challenge_for_active_run()):
            return blocked('challenge_required','Complete the active employer verification before starting another application.')
        return blocked('worker_busy','Another JobPilot task is running.')
    if external_worker_is_busy():
        return blocked('worker_busy','A JobPilot command-line task is already running. Wait for it to finish before starting another application.')
    personal=profile.get('personal',{})
    missing=[key for key in ('firstName','lastName','email','phone') if not str(personal.get(key) or '').strip()]
    if missing: return blocked('missing_profile',f"Complete candidate profile fields: {', '.join(missing)}.")
    if 'unanswered_count' in job.keys() and int(job['unanswered_count'] or 0):
        return blocked('missing_answer',f"Resolve {int(job['unanswered_count'])} unanswered screening question(s) before applying.")
    reason=str(job['eligibility_reason'] or '')
    if 'job closed' in reason.casefold() or 'job expired' in reason.casefold():
        return blocked('job_closed_or_stale',reason)
    parsed=urlparse(str(job['apply_url'] or ''))
    if parsed.scheme not in ('https','http') or not parsed.hostname:
        return blocked('job_closed_or_stale','The job has no valid employer application URL.')
    try:
        eligible, eligibility_reason=jobpilot.eligible(job,rules)
    except Exception:
        eligible,eligibility_reason=False,'Eligibility could not be verified from the saved job data.'
    pending = 'source' in job.keys() and job['source']=='custom' and not job['company']
    pasted = 'application_mode' in job.keys() and job['application_mode']=='USER_LINK'
    if str(job['ats'] or '').lower()=='naukri' and not pasted and jobpilot.naukri_quota_active():
        return blocked('naukri_quota','Naukri daily apply quota exhausted; paused until next daily reset.')
    if not eligible and not pending: return blocked('eligibility_review',eligibility_reason)
    if not pasted:
        try:
            fit=jobpilot.match_decision(job,profile,jobpilot.load_json('skills.json'),rules)
        except Exception:
            return blocked('eligibility_review','Candidate and job match could not be verified.')
        minimum=float(settings.get('autoApplyMinimumScore',60))
        if fit.score <= minimum:
            return blocked('low_match_score',f'Match score {fit.score:g} must be above the auto-apply minimum ({minimum:g}).')
    resume_name=str(job['resume'] or 'generic.pdf')
    resume_root=(jobpilot.DATA/'resumes').resolve()
    resume_path=(resume_root/resume_name).resolve()
    if not pending and (resume_path.parent != resume_root or resume_path.suffix.lower() != '.pdf' or not resume_path.is_file()):
        return blocked('resume_missing',f'Selected resume is unavailable: {resume_name}.')
    if queue:
        maximum,_,_=retry_policy(settings)
        if int(queue['attempts'] or 0)>=maximum: return blocked('attempt_limit','Application attempt limit reached; review saved evidence.')
        if not jobpilot.due(queue['next_attempt_at'],jobpilot.now()): return blocked('retry_not_due',f"Retry is scheduled for {queue['next_attempt_at']}.")
    return {'ready':True,'reason_code':'ready','message':'Ready to apply. This click submits this selected job.'}


@app.get('/api/jobs')
def jobs(hide_applied: bool = True):
    where_clause = "WHERE j.status <> 'Applied'" if hide_applied else ""
    rows=query(f'''SELECT j.id,j.company,j.title,j.description,j.location,j.score,j.status,j.eligibility_reason,j.apply_url,j.ats,j.resume,j.posted_at,j.source,
        q.attempts,q.next_attempt_at,q.status AS queue_status,q.mode AS application_mode,
        (SELECT COUNT(*) FROM unknown_questions u WHERE u.job_id=j.id AND (u.answer IS NULL OR TRIM(u.answer)='')) AS unanswered_count,
        EXISTS(SELECT 1 FROM application_attempts a WHERE a.job_id=j.id AND a.step='submit') AS submitted,
        (SELECT MAX(completed_at) FROM application_attempts a WHERE a.job_id=j.id AND a.status='Applied') AS applied_at
        FROM jobs j LEFT JOIN application_queue q ON q.job_id=j.id
        {where_clause}
        ORDER BY CASE WHEN j.status='Queued' THEN 0 ELSE 1 END,j.score DESC LIMIT 150''')
    settings=jobpilot.load_json('settings.json');profile=jobpilot.load_json('profile.json');rules=jobpilot.load_json('eligibility.json')
    maximum,_,_=retry_policy(settings)
    challenge_waiting=waiting_challenge_for_active_run()
    active_action=None
    if _active:
        active_run=query('SELECT action FROM agent_runs WHERE id=?',(_active,))
        active_action=active_run[0]['action'] if active_run else None
    for row in rows:
        fit=jobpilot.match_decision(row,profile=profile,rules=rules)
        row['match_explanation']=fit.explanation
        row['match_breakdown']=asdict(fit.score_breakdown)
        row['recommended_resume']=fit.recommended_resume
        readiness=submission_readiness(row,{'attempts':row['attempts'],'next_attempt_at':row['next_attempt_at'],'status':row['queue_status']},
                                       bool(row['submitted']),settings,profile,rules,challenge_waiting)
        reason=str(row['eligibility_reason'] or '')
        row_is_currently_eligible, current_reason=jobpilot.eligible(row,rules)
        if row['status'] in ('Discovered','Qualified') and (not row_is_currently_eligible or fit.score < float(rules.get('minimumScore',50))):
            row['readiness']={'ready':False,'reason_code':'eligibility_review','message':current_reason if not row_is_currently_eligible else f'Match score {fit.score:g} is below the configured minimum ({rules.get("minimumScore",50)}).'}
        else:
            if _active and active_action not in ('apply','custom-link','login') and not external_worker_is_busy():
                readiness=jobpilot_readiness_without_worker(row,settings,profile,rules)
            row['readiness']=readiness
        row['can_submit']=row['readiness']['ready']
        row['recovery']=recovery_guidance(row['status'],row['eligibility_reason'],bool(row['submitted']))
        row['recovery']['automatic_retry']=bool(row['recovery']['automatic_retry'] and row['next_attempt_at'] and (row['attempts'] or 0)<maximum)
        if (row['attempts'] or 0)>=maximum and row['status']!='Applied':
            row['recovery']=recovery_guidance(row['status'],'Application attempt limit reached',bool(row['submitted']))
    attach_application_states(rows)
    return rows


@app.get('/api/pipeline/preview')
def pipeline_preview(step: str, limit: int = 5):
    """Read-only preview of an explicit scoring or queueing step."""
    if step not in ('score', 'queue'):
        raise HTTPException(422, "step must be 'score' or 'queue'")
    if not 1 <= limit <= 100:
        raise HTTPException(422, 'limit must be between 1 and 100')
    db=jobpilot.connect()
    try:
        if step == 'score':
            profile=jobpilot.load_json('profile.json');rules=jobpilot.load_json('eligibility.json');skills=jobpilot.load_json('skills.json')
            candidates=db.execute("""SELECT * FROM jobs WHERE status IN ('Discovered','Qualified','Skipped')
                AND COALESCE(eligibility_reason,'') NOT LIKE 'Job closed%' ORDER BY discovered_at DESC""").fetchall()
            proposed=[]
            for row in candidates:
                ok,reason=jobpilot.eligible(row,rules)
                value=jobpilot.score(row,profile,skills) if ok else 0
                status='Qualified' if ok and value >= rules.get('minimumScore',50) else 'Skipped'
                resume=jobpilot.application_resume_for(row['title']+' '+row['description'], int(row['id'])) if status == 'Qualified' else jobpilot.application_resume_for(row['title']+' '+row['description'])
                priority=jobpilot.priority(value) if value else None
                if (row['status'] != status or row['score'] != value or row['eligibility_reason'] != reason
                    or bool(row['eligible']) != bool(ok) or row['priority'] != priority or row['resume'] != resume):
                    proposed.append({'job_id':row['id'],'company':row['company'],'title':row['title'],
                                     'current_status':row['status'],'proposed_status':status,
                                     'score':value,'resume':resume,'reason':reason})
            return {'step':step,'candidate_count':len(candidates),'change_count':len(proposed),
                    'items':proposed[:limit],'truncated':len(proposed)>limit}
        candidates=jobpilot.queue_candidates(db,limit)
        return {'step':step,'target_count':limit,'queue_count':len(candidates),
                'items':[{'job_id':r['id'],'company':r['company'],'title':r['title'],
                          'location':r['location'],'score':r['score']} for r in candidates]}
    finally:
        db.close()


def application_row(db, job_id):
    return db.execute('''SELECT j.*,q.attempts,q.next_attempt_at,q.status AS queue_status,q.mode AS application_mode,
        (SELECT COUNT(*) FROM unknown_questions u WHERE u.job_id=j.id AND (u.answer IS NULL OR TRIM(u.answer)='')) AS unanswered_count,
        EXISTS(SELECT 1 FROM application_attempts a WHERE a.job_id=j.id AND a.step='submit') AS submitted
        FROM jobs j LEFT JOIN application_queue q ON q.job_id=j.id WHERE j.id=?''',(job_id,)).fetchone()


def application_readiness(row):
    return submission_readiness(row,{'attempts':row['attempts'],'next_attempt_at':row['next_attempt_at'],
                                     'status':row['queue_status']},bool(row['submitted']))


def jobpilot_readiness_without_worker(row, settings, profile, rules):
    """Evaluate readiness without a false busy result during a non-apply run."""
    from unittest.mock import patch
    with patch('agent_server._active',None), patch('agent_server.external_worker_is_busy',return_value=False):
        return submission_readiness(row,
            {'attempts':row['attempts'],'next_attempt_at':row['next_attempt_at'],'status':row['queue_status']},
            bool(row['submitted']),settings,profile,rules,False)


@app.get('/api/apply/preview')
def apply_preview(limit: int = 5):
    """Show the exact ready jobs a batch click may submit."""
    if not 1 <= limit <= 100:raise HTTPException(422,'limit must be between 1 and 100')
    settings=jobpilot.load_json('settings.json')
    effective_limit=min(limit,max(1,int(settings.get('bulkBatchSize',limit))))
    db=jobpilot.connect()
    try:
        candidates=db.execute('''SELECT j.id FROM application_queue q JOIN jobs j ON j.id=q.job_id
            WHERE q.status='Queued' AND j.status='Queued'
            ORDER BY (q.mode='USER_LINK') DESC,COALESCE(q.score,j.score,0) DESC,j.id LIMIT 1000''').fetchall()
        items=[]
        blocked = Counter()
        scanned = 0
        for candidate in candidates:
            scanned += 1
            row=application_row(db,candidate['id'])
            decision = application_readiness(row) if row else {'ready': False, 'reason_code': 'listing_changed'}
            if not decision['ready']:
                blocked[decision['reason_code']] += 1
                continue
            items.append({'job_id':row['id'],'company':row['company'],'title':row['title'],
                          'location':row['location'],'score':row['score'],
                          'resume':row['resume'] or 'generic.pdf'})
            if len(items)==effective_limit:break
        return {'limit':effective_limit,'requested_limit':limit,'items':items,'count':len(items),'batch_cap':effective_limit,
                'scanned':scanned,'blocked':dict(blocked),'scan_limit_reached':len(candidates)==1000}
    finally:db.close()


@app.get('/api/profile')
def profile():return {'profile':jobpilot.load_json('profile.json'),'answers':jobpilot.load_json('answers.json'),
                      'revision':jobpilot.candidate_config_revision()}


def parse_career_board_url(raw_url: str, company_name: str) -> dict:
    """Accept employer-facing URLs for public ATS job boards JobPilot can query."""
    parsed=urlparse(raw_url.strip())
    host=(parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme!='https' or not host or parsed.username or parsed.password:
        raise HTTPException(422,'Use an HTTPS public employer or ATS careers page URL.')
    parts=[part for part in parsed.path.split('/') if part]
    ats=board=tenant=domain=wd_board=None
    if host in ('boards.greenhouse.io','job-boards.greenhouse.io') and parts:
        ats,board='greenhouse',parts[0]
    elif host in ('jobs.lever.co','jobs.eu.lever.co') and parts:
        ats,board='lever',parts[0]
    elif host=='jobs.ashbyhq.com' and parts:
        ats,board='ashby',parts[0]
    elif host=='jobs.smartrecruiters.com' and parts:
        ats,board='smartrecruiters',parts[0]
    elif host=='apply.workable.com' and parts:
        ats,board='workable',parts[0]
    elif host.endswith('.myworkdayjobs.com'):
        match=re.match(r'^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$',host)
        if match and len(parts)>=2:
            ats,tenant,domain,wd_board='workday',match.group(1),host,parts[1]
            board=tenant
    if not ats or not board:
        raise HTTPException(422,'This page is not a supported public job board. Supported sources: Greenhouse, Lever, Ashby, SmartRecruiters, Workable and Workday.')
    entry={'company':company_name.strip(),'ats':ats,'boardId':board,'enabled':True}
    if ats=='workday':entry.update({'tenant':tenant,'domain':domain,'board':wd_board,'searchText':'Software Engineer India'})
    return entry


@app.get('/api/employers')
def employers():
    curated=jobpilot.load_json('companies.json')
    custom_path=jobpilot.DATA/'employer-sources.json'
    try:custom=json.loads(custom_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):custom=[]
    return {'supported_ats':sorted(jobpilot.SOURCES.keys()),'curated_count':len(curated),
            'custom_sources':custom if isinstance(custom,list) else []}


class CareerSourceInput(BaseModel):
    company:str=Field(min_length=1,max_length=120)
    url:str=Field(min_length=8,max_length=1000)


@app.post('/api/employers')
def add_employer(payload:CareerSourceInput):
    company=payload.company.strip()
    if not company:raise HTTPException(422,'Enter an employer name.')
    entry=parse_career_board_url(payload.url,company)
    path=jobpilot.DATA/'employer-sources.json'
    try:items=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):items=[]
    if not isinstance(items,list):items=[]
    existing={(str(item.get('ats','')).lower(),str(item.get('boardId','')).casefold()) for item in items if isinstance(item,dict)}
    key=(entry['ats'],entry['boardId'].casefold())
    if key in existing:return {'added':False,'duplicate':True,'source':entry}
    items.append(entry)
    atomic_json(path,items)
    return {'added':True,'duplicate':False,'source':entry,'total_custom_sources':len(items)}


@app.get('/api/captcha-report')
def captcha_report():
    path=jobpilot.DATA/'captcha-backtest.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'state':'Not tested'}


@app.put('/api/profile')
async def save_profile(request:Request):
    if _active:raise HTTPException(409,'Stop the worker before editing the full profile')
    data=await request.json()
    if not isinstance(data,dict) or not isinstance(data.get('profile'),dict) or not isinstance(data.get('answers'),dict):
        raise HTTPException(422,'Profile and answers must be objects')
    personal=data['profile'].get('personal',{})
    if not isinstance(personal,dict) or any(not str(personal.get(k,'')).strip() for k in ('firstName','lastName','email','phone')):
        raise HTTPException(422,'Complete name, email and phone')
    try:revision=jobpilot.write_candidate_config(data['profile'],data['answers'],data.get('revision'))
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    except WorkerBusy as exc:raise HTTPException(409,'Candidate answers are being saved by another JobPilot process; try again') from exc
    return {'saved':True,'revision':revision}


@app.get('/api/questions')
def questions():return query("SELECT * FROM unknown_questions WHERE answer IS NULL ORDER BY id DESC LIMIT 100")


class AnswerInput(BaseModel):
    answer:str=Field(min_length=1,max_length=10000)


@app.post('/api/questions/{question_id}')
def save_answer(question_id:int, payload:AnswerInput):
    rows=query('SELECT * FROM unknown_questions WHERE id=?',(question_id,))
    if not rows:raise HTTPException(404,'Question not found')
    if rows[0]['question'].strip().casefold()=='unknown question' or rows[0]['question'].startswith('cards['):
        raise HTTPException(422,'Inspect the job screenshot and supply the actual question wording before saving a reusable answer')
    from argparse import Namespace
    try:jobpilot.cmd_remember_answer(Namespace(question=rows[0]['question'],answer=payload.answer))
    except WorkerBusy as exc:raise HTTPException(409,'Candidate answers are being saved by another JobPilot process; try again') from exc
    db=jobpilot.connect()
    db.execute('UPDATE unknown_questions SET answer=? WHERE id=?',(payload.answer,question_id)); db.commit();db.close()
    return {'saved':True}


class RunInput(BaseModel):
    action:str
    limit:int=Field(default=5,ge=1,le=100)
    job:int|None=Field(default=None,ge=1)
    job_ids:list[int]|None=Field(default=None,max_length=100)
    dry_run:bool=True
    url:str|None=None


class ChatInput(BaseModel):
    message:str=Field(min_length=1,max_length=20000)


class MemoryInput(BaseModel):
    bucket:str=Field(pattern='^(preferences|lessons)$')
    note:str=Field(min_length=1,max_length=500)


def memory_file():
    return jobpilot.DATA/'agent-memory.json'


def _chat_memory():
    try:return json.loads(memory_file().read_text(encoding='utf-8'))
    except (OSError,ValueError):return {'preferences':[],'lessons':[]}


def _save_chat_memory(memory):
    atomic_json(memory_file(),memory)


@app.get('/api/memory')
def memory():
    return _chat_memory()


@app.post('/api/memory')
def add_memory(payload:MemoryInput):
    current=_chat_memory(); current.setdefault(payload.bucket,[])
    if payload.note not in current[payload.bucket]: current[payload.bucket].append(payload.note)
    _save_chat_memory(current)
    return current


def extract_job_urls(text: str) -> list[str]:
    """Extract unique HTTP(S) job URLs from a freeform chat message in order."""
    urls = []
    seen = set()
    for raw in re.findall(r'https?://[^\s<>"\'`]+', text, re.I):
        url = raw.rstrip('.,;:!?\'">')
        while url.endswith(')') and url.count(')') > url.count('('):
            url = url[:-1]
        while url.endswith(']') and url.count(']') > url.count('['):
            url = url[:-1]
        url = url.rstrip('.,;:!?\'">')
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _apply_pasted_links(urls: list[str], request: Request, memory: dict) -> dict:
    """Import extracted job links, check readiness, and start applying to ready jobs."""
    items = []
    ready_ids = []
    seen_ready = set()
    queued_not_ready = []
    for url in urls:
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.hostname.lower() in ('localhost', '127.0.0.1', '::1')):
            items.append({'url': url, 'job_id': None, 'status': 'Invalid',
                          'reason': 'Provide a public HTTPS job application link.', 'ready': False})
            continue
        try:
            job_id, status, reason = jobpilot.import_custom_job(url)
        except Exception as exc:
            items.append({'url': url, 'job_id': None, 'status': 'Invalid',
                          'reason': str(exc) or 'Could not import job link.', 'ready': False})
            continue
        db = jobpilot.connect()
        try:
            row = application_row(db, job_id)
            readiness = application_readiness(row) if row else {
                'ready': False, 'reason_code': 'not_found', 'message': 'Job record not found.'}
        finally:
            db.close()
        item = {'url': url, 'job_id': job_id, 'status': status, 'reason': reason,
                'ready': readiness['ready'], 'readiness_code': readiness['reason_code'],
                'readiness_message': readiness['message']}
        items.append(item)
        if readiness['ready'] and job_id not in seen_ready:
            seen_ready.add(job_id)
            ready_ids.append(job_id)
        elif status == 'Queued' and not readiness['ready']:
            queued_not_ready.append(item)

    run = None
    run_error = None
    batch_ids = ready_ids[:100]
    if batch_ids:
        try:
            run = start(RunInput(action='apply', limit=len(batch_ids), job_ids=batch_ids, dry_run=False), request)
        except HTTPException as exc:
            run_error = exc.detail

    lines = []
    if run:
        id_list = ', '.join(f'#{jid}' for jid in batch_ids)
        extra = f' ({len(ready_ids) - len(batch_ids)} more queued for the next batch)' if len(ready_ids) > len(batch_ids) else ''
        lines.append(f'Retrieved {len(urls)} link(s) and started applying to {len(batch_ids)} job(s): {id_list}.{extra}')
    elif batch_ids and run_error:
        lines.append(f'Retrieved {len(urls)} link(s) and queued {len(batch_ids)} job(s), but could not start applying right now: {run_error}')
    elif queued_not_ready:
        id_list = ', '.join(f"#{i['job_id']}" for i in queued_not_ready)
        lines.append(f'Retrieved {len(urls)} link(s) and queued {len(queued_not_ready)} job(s) ({id_list}): {queued_not_ready[0]["readiness_message"]}')
    else:
        lines.append(f'Retrieved {len(urls)} link(s), but none are ready for auto-apply.')

    for item in items:
        if not item['ready']:
            label = f"#{item['job_id']}" if item['job_id'] else item['url']
            detail = item.get('readiness_message') if item['status'] == 'Queued' else (item['reason'] or item['status'])
            lines.append(f"- {label} ({item['status']}): {detail}")

    return {
        'reply': '\n'.join(lines),
        'jobs': items,
        'job': items[0] if len(items) == 1 else None,
        'run': run,
        'memory': memory,
        'action': 'apply' if run else 'custom-link',
    }


@app.post('/api/chat')
def chat(payload:ChatInput,request:Request):
    """Conversational local control surface with auditable actions."""
    text=payload.message.strip(); lower=text.casefold(); memory=_chat_memory()
    urls=extract_job_urls(text)
    if urls:
        return _apply_pasted_links(urls, request, memory)
    lookup=re.fullmatch(r'(?:status(?: of)?|check)\s+(?:job\s*)?#?(\d+)',lower)
    if lookup:
        rows=query('SELECT id,status,eligibility_reason FROM jobs WHERE id=?',(int(lookup[1]),))
        reply=(f"Job #{rows[0]['id']} · {rows[0]['status']}. {rows[0]['eligibility_reason'] or ''}"
               if rows else 'Job ID not found.')
        return {'reply':reply,'memory':memory,'action':None}
    import email_diary_service
    diary_res=email_diary_service.handle_chat_diary_command(text)
    if diary_res:
        diary_res['memory']=memory
        return diary_res
    if lower.startswith(('remember ','learn ')):
        note=text.split(' ',1)[1].strip()
        bucket='lessons' if lower.startswith('learn ') else 'preferences'
        memory.setdefault(bucket,[])
        if note and note not in memory[bucket]: memory[bucket].append(note); _save_chat_memory(memory)
        return {'reply':f'I saved that {"lesson" if bucket == "lessons" else "preference"}: {note}','memory':memory,'action':None}
    if any(word in lower for word in ('top matches','best matches','recommend jobs','what should i apply')):
        rows=query("SELECT id,title,company,location,score,ats,eligibility_reason FROM jobs WHERE status='Qualified' ORDER BY score DESC,id LIMIT 5")
        if not rows:return {'reply':'There are no qualified matches yet. Ask me to find jobs, then refresh the queue.','memory':memory,'action':None}
        summary='; '.join(f"#{r['id']} {r['title']} at {r['company']} ({r['score']:.0f}/100, {r['location']})" for r in rows)
        return {'reply':f'Top current matches: {summary}. Ask “explain job {rows[0]["id"]}” for the strongest match.','matches':rows,'memory':memory,'action':None}
    if lower.startswith(('explain job ','why job ')):
        match=__import__('re').search(r'(\d+)',lower)
        if match:
            rows=query("SELECT id,title,company,description,location,score,eligibility_reason,ats FROM jobs WHERE id=?",(int(match.group(1)),))
            if rows:
                row=rows[0]; return {'reply':f"{row['title']} at {row['company']} scores {row['score'] or 0:.0f}/100. Location: {row['location'] or 'unknown'}. Matching note: {row['eligibility_reason'] or 'qualified by profile rules'}. Source: {row['ats'] or 'employer'}.",'job':row,'memory':memory,'action':None}
        return {'reply':'I could not find that job ID in the tracker.','memory':memory,'action':None}
    if any(word in lower for word in ('blockers','what is blocked','needs attention','review items')):
        rows=query("SELECT status,COUNT(*) AS count FROM jobs WHERE status IN ('ManualReview','SkippedBlocked','FailedTemporary') GROUP BY status")
        summary=', '.join(f"{r['count']} {r['status']}" for r in rows) or 'nothing is currently blocked'
        return {'reply':f'Current attention items: {summary}.','memory':memory,'action':None}
    if any(word in lower for word in ('status','how many','queue','applied')):
        snapshot=state(); counts={row['status']:row['count'] for row in snapshot.get('counts',[])}; periods=snapshot.get('applied_periods',{})
        return {'reply':f"You have {periods.get('today',0)} applications today, {periods.get('before_today',0)} earlier confirmed applications, {counts.get('Queued',0)} queued jobs, and {counts.get('ManualReview',0)} needing review.",'memory':memory,'action':None}
    action=None
    if any(word in lower for word in ('find jobs','discover jobs','search jobs','refresh jobs')): action='discover'
    elif any(word in lower for word in ('queue jobs','prepare applications','refresh queue')): action='queue'
    elif any(word in lower for word in ('apply to ready','start applications','apply ready','bulk apply','apply in bulk')): action='apply'
    if action:
        if action=='apply':
            ready=apply_preview(limit=10).get('items',[])
            ids=[int(item['job_id']) for item in ready if item.get('job_id')]
            if not ids:return {'reply':'There are no ready applications with a validated submit-and-confirm adapter. I left the queue unchanged.','memory':memory,'action':None}
            result=start(RunInput(action='apply',limit=len(ids),job_ids=ids,dry_run=False),request)
        else:
            result=start(RunInput(action=action,limit=10),request)
        return {'reply':f'I started {action}. I will keep receipts and surface anything needing review.','memory':memory,'action':action,'run':result}
    return {'reply':'I can check status, find jobs, refresh the queue, start ready applications, or remember a preference. Try “find jobs” or “remember I prefer remote backend roles”.','memory':memory,'action':None}


class ContinueInput(BaseModel):
    challenge_id:str=Field(min_length=1,max_length=256)


def supervise(run_id, command, log_path, stop_path):
    global _active,_process
    code=-1
    status='Failed'
    try:
        env=os.environ.copy();env.update(PYTHONUTF8='1',PYTHONUNBUFFERED='1',JOBPILOT_STOP_FILE=str(stop_path),JOBPILOT_CONTINUE_FILE=str(stop_path.with_suffix('.continue')),JOBPILOT_DASHBOARD='1',JOBPILOT_RUN_ID=run_id)
        with log_path.open('w',encoding='utf-8') as log:
            _process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            code=_process.wait()
        status='Stopped' if stop_path.exists() else 'Completed' if code==0 else 'Failed'
    except Exception as exc:
        try:
            with log_path.open('a',encoding='utf-8') as log:log.write(f'\nSupervisor error: {type(exc).__name__}: {exc}\n')
        except OSError:
            pass
    finally:
        try:
            cleanup_errors=[]
            challenge_path=jobpilot.DATA/'captcha_challenge.json'
            try:
                if challenge_path.is_file():
                    challenge=json.loads(challenge_path.read_text(encoding='utf-8'))
                    if challenge.get('run_id')==run_id and challenge.get('status')=='WaitingForUser':
                        challenge['status']='Interrupted';challenge['updated_at']=jobpilot.now()
                        from application_support import atomic_json
                        atomic_json(challenge_path,challenge)
            except Exception as exc:cleanup_errors.append(f'challenge cleanup: {type(exc).__name__}: {exc}')
            for signal_path in (stop_path.with_suffix('.code'),stop_path.with_suffix('.continue')):
                try:signal_path.unlink(missing_ok=True)
                except OSError as exc:cleanup_errors.append(f'{signal_path.name}: {exc}')
            try:recover_idle_attempts()
            except Exception as exc:cleanup_errors.append(f'attempt recovery: {type(exc).__name__}: {exc}')
            updated = False
            for db_attempt in range(10):
                try:
                    db = jobpilot.connect()
                    try:
                        if cleanup_errors and status == 'Completed':
                            status = 'Failed'
                        db.execute('UPDATE agent_runs SET status=?,completed_at=?,exit_code=? WHERE id=?',
                                   (status, jobpilot.now(), code if not cleanup_errors else -1, run_id))
                        db.commit()
                        updated = True
                        break
                    finally:
                        db.close()
                except Exception as exc:
                    time.sleep(1)
            if not updated:
                cleanup_errors.append('run record: update timed out after 10 retries')
            if cleanup_errors:
                try:
                    with log_path.open('a',encoding='utf-8') as log:
                        log.write('Supervisor cleanup errors: '+'; '.join(cleanup_errors)+'\n')
                except OSError:
                    pass
        finally:
            with _guard:
                if _active==run_id:_active=None
                _process=None


@app.post('/api/runs')
def start(payload:RunInput,request:Request):
    global _active
    if pause_file().exists() and request.headers.get('x-jobpilot-autonomous')=='true':
        raise HTTPException(409,'Autonomous work is paused. Resume it in the dashboard first.')
    if payload.action not in ('discover','score','queue','boards','naukri','remotive','himalayas','portals','custom-link','apply','login','retry'):raise HTTPException(422,'Unsupported action')
    if payload.action=='login':
        parsed=urlparse(payload.url or '')
        if parsed.scheme!='https' or not parsed.hostname:raise HTTPException(422,'Use an HTTPS portal login URL')
    if payload.action=='custom-link':
        parsed=urlparse(payload.url or '')
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(422,'Provide one public HTTPS job application link.')
        if parsed.hostname.lower() in ('localhost','127.0.0.1','::1'):
            raise HTTPException(422,'Local computer links cannot be used as employer application pages.')
        job_id,status,reason=jobpilot.import_custom_job(payload.url)
        return {'job_id':job_id,'status':status,'reason':reason}
    if payload.job_ids is not None:
        if payload.action!='apply' or payload.job or not payload.job_ids or len(payload.job_ids)>payload.limit:
            raise HTTPException(422,'Select between 1 and the batch limit of job IDs for Apply')
        if any(job_id<1 for job_id in payload.job_ids) or len(set(payload.job_ids))!=len(payload.job_ids):
            raise HTTPException(422,'Selected job IDs must be positive and unique')
    if payload.action=='apply' and not payload.dry_run and not (payload.job or payload.job_ids):
        raise HTTPException(422,'Select a job or review a ready batch before applying')
    if payload.action=='apply' and payload.job and not payload.dry_run:
        db=jobpilot.connect()
        try:
            row=application_row(db,payload.job)
            if not row:raise HTTPException(404,'Job not found')
            readiness=application_readiness(row)
            if not readiness['ready']:raise HTTPException(409,readiness['message'])
        finally:db.close()
    if payload.action=='apply' and payload.job_ids and not payload.dry_run:
        db=jobpilot.connect()
        try:
            for job_id in payload.job_ids:
                row=application_row(db,job_id)
                if not row:raise HTTPException(404,f'Job {job_id} not found')
                readiness=application_readiness(row)
                if not readiness['ready']:raise HTTPException(409,f"Job {job_id}: {readiness['message']}")
        finally:db.close()
    with _guard:
        if backend_build() != LOADED_BUILD:
            raise HTTPException(409,'JobPilot code changed. Restart the local service before starting another task.')
        if _active:raise HTTPException(409,'A worker is already running')
        if external_worker_is_busy():raise HTTPException(409,'A JobPilot command-line worker is already running')
        (jobpilot.DATA/'captcha_challenge.json').unlink(missing_ok=True)
        run_id=uuid.uuid4().hex
        run_dir=jobpilot.DATA/'agent-runs';run_dir.mkdir(parents=True,exist_ok=True)
        log_path=run_dir/f'{run_id}.log';stop_path=run_dir/f'{run_id}.stop'
        python=sys.executable
        command=[python,'-X','utf8',str(ROOT/'agent_worker.py'),payload.action,'--limit',str(payload.limit)]
        if payload.job:command+=['--job',str(payload.job)]
        if payload.job_ids:command+=['--jobs',','.join(map(str,payload.job_ids))]
        if payload.dry_run:command+=['--dry-run']
        if payload.url:command+=['--url',payload.url]
        if request.headers.get('x-jobpilot-autonomous')=='true':command+=['--autonomous']
        started = time.monotonic()
        db = jobpilot.connect()
        try:
            db.execute('PRAGMA busy_timeout=3000')
            with db:
                db.execute('INSERT INTO agent_runs(id,action,status,started_at,log_path) VALUES(?,?,?,?,?)',
                           (run_id, payload.action, 'Running', jobpilot.now(), str(log_path)))
        except sqlite3.OperationalError as exc:
            code = getattr(exc, 'sqlite_errorname', '')
            lock_error = code.startswith(('SQLITE_BUSY', 'SQLITE_LOCKED')) or any(
                word in str(exc).lower() for word in ('locked', 'busy')
            )
            if not lock_error:
                raise
            logging.warning('Run creation blocked by SQLite action=%s autonomous=%s elapsed=%.3fs',
                            payload.action, request.headers.get('x-jobpilot-autonomous') == 'true',
                            time.monotonic() - started)
            raise HTTPException(503, 'JobPilot database is busy. Try again shortly.', headers={'Retry-After': '3'}) from exc
        finally:
            db.close()
        _active=run_id
        try:
            threading.Thread(target=supervise,args=(run_id,command,log_path,stop_path),daemon=True).start()
        except Exception:
            _active=None
            db=jobpilot.connect()
            try:
                db.execute("UPDATE agent_runs SET status='Failed',completed_at=?,exit_code=-1 WHERE id=?",(jobpilot.now(),run_id))
                db.commit()
            finally:db.close()
            raise HTTPException(500,'Worker could not start; try again')
    return {'id':run_id}


@app.post('/api/stop')
def stop():
    pause_file().parent.mkdir(parents=True,exist_ok=True)
    pause_file().write_text(jobpilot.now(),encoding='utf-8')
    if not _active:return {'stopping':False,'autonomy_paused':True}
    (jobpilot.DATA/'agent-runs'/f'{_active}.stop').write_text('stop',encoding='utf-8')
    db=jobpilot.connect();db.execute("UPDATE agent_runs SET status='StopRequested' WHERE id=?",(_active,));db.commit();db.close()
    return {'stopping':True,'autonomy_paused':True}


@app.post('/api/autonomy/resume')
def resume_autonomy():
    pause_file().unlink(missing_ok=True)
    return {'autonomy_paused':False}


@app.post('/api/continue')
def continue_worker(payload:ContinueInput):
    with _guard:
        if not _active:raise HTTPException(409,'No application worker is running')
        challenge_path=jobpilot.DATA/'captcha_challenge.json'
        try:challenge=json.loads(challenge_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):raise HTTPException(409,'No active verification challenge is waiting')
        if challenge.get('status')!='WaitingForUser' or challenge.get('run_id')!=_active:
            raise HTTPException(409,'The verification challenge is no longer active for this worker')
        if challenge.get('challenge_id')!=payload.challenge_id:
            raise HTTPException(409,'This verification request belongs to an older challenge')
        signal=jobpilot.DATA/'agent-runs'/f'{_active}.continue'
        signal.parent.mkdir(parents=True,exist_ok=True)
        signal.write_text(payload.challenge_id,encoding='utf-8')
    return {'continuing':True,'challenge_id':payload.challenge_id}


@app.get('/api/captcha-status')
def captcha_status():
    flag=jobpilot.DATA/'captcha_challenge.json'
    if flag.is_file():
        try:
            return json.loads(flag.read_text(encoding='utf-8'))
        except Exception:
            return {'status':'WaitingForUser'}
    return {'status':'Clear'}


class VerificationInput(BaseModel):
    code:str=Field(pattern=r'^[A-Za-z0-9]{4,12}$')


@app.post('/api/verification')
def verification(payload:VerificationInput):
    if not _active:raise HTTPException(409,'No application worker is running')
    (jobpilot.DATA/'agent-runs'/f'{_active}.code').write_text(payload.code,encoding='utf-8')
    return {'received':True}


@app.get('/api/runs/{run_id}/log')
def log(run_id:str):
    rows=query('SELECT log_path FROM agent_runs WHERE id=?',(run_id,))
    if not rows:raise HTTPException(404,'Run not found')
    path=Path(rows[0]['log_path'])
    return {'text':path.read_text(encoding='utf-8',errors='replace')[-30000:] if path.exists() else ''}


@app.get('/api/evidence/{job_id}/{kind}')
def evidence(job_id:int,kind:str,attempt_id:int|None=None):
    suffix={'image':'filled.png','questions':'questions.json','naukri-answers':'naukri-answers.json',
            'posting':'posting.json','trace':'trace.zip',
            'receipt':'receipt.json','attempt':'attempt.json','checkpoint':'checkpoint.json','resume':'resume.pdf','controls':'submission-controls.json'}.get(kind)
    if not suffix:raise HTTPException(404,'Evidence type not found')
    if attempt_id is not None:
        rows=query('SELECT id,screenshot_path FROM application_attempts WHERE job_id=? AND id=?',(job_id,attempt_id))
    else:
        rows=query('SELECT id,screenshot_path FROM application_attempts WHERE job_id=? ORDER BY id DESC LIMIT 1',(job_id,))
    if rows:
        path=jobpilot.DATA/'screenshots'/f'job-{job_id}'/f"attempt-{rows[0]['id']}"/suffix
        legacy=rows[0]['screenshot_path'] and Path(rows[0]['screenshot_path']).name==f'job-{job_id}-filled.png'
        if not path.is_file() and legacy and attempt_id is None:
            path=jobpilot.DATA/'screenshots'/f'job-{job_id}-{suffix}'
    elif attempt_id is not None:
        raise HTTPException(404,'Attempt not found for this job')
    else:
        path=jobpilot.DATA/'screenshots'/f'job-{job_id}-{suffix}'
    if not path.is_file():raise HTTPException(404,'No evidence saved yet')
    return FileResponse(path)


@app.get('/api/jobs/{job_id}/attempts')
def application_attempts(job_id:int):
    if not query('SELECT id FROM jobs WHERE id=?',(job_id,)):
        raise HTTPException(404,'Job not found')
    rows=query('SELECT * FROM application_attempts WHERE job_id=? ORDER BY id DESC',(job_id,))
    for row in rows:
        row['recovery']=recovery_guidance(row['status'],row['error'],row['step']=='submit')
        directory=jobpilot.DATA/'screenshots'/f'job-{job_id}'/f"attempt-{row['id']}"
        row['evidence']={kind:f"/api/evidence/{job_id}/{kind}?attempt_id={row['id']}"
                         for kind,filename in {'image':'filled.png','trace':'trace.zip','questions':'questions.json',
                                               'naukri-answers':'naukri-answers.json','posting':'posting.json',
                                               'receipt':'receipt.json','attempt':'attempt.json','checkpoint':'checkpoint.json','resume':'resume.pdf','controls':'submission-controls.json'}.items()
                         if (directory/filename).is_file()}
    return rows


@app.get('/api/jobs/today')
def jobs_today(hide_applied: bool = True):
    """Return jobs discovered in the last 24 hours, ordered by score desc."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    where_applied = " AND status <> 'Applied'" if hide_applied else ""
    rows = query(
        f"SELECT id, title, company, description, location, ats AS portal, score, status, apply_url, source_url, posted_at, discovered_at AS created_at"
        f" FROM jobs WHERE discovered_at > ? AND status NOT IN ('Skipped', 'SkippedBlocked', 'Rejected'){where_applied}"
        f" ORDER BY score DESC LIMIT 200",
        (cutoff,)
    )
    profile=jobpilot.load_json('profile.json')
    skills=jobpilot.load_json('skills.json')
    rules=jobpilot.load_json('eligibility.json')
    for row in rows:
        fit=jobpilot.match_decision(row,profile,skills,rules)
        row['score']=fit.score
        row['match_explanation']=fit.explanation
        row['match_breakdown']=asdict(fit.score_breakdown)
        row['recommended_resume']=fit.recommended_resume
        reasons=[]
        if fit.score_breakdown.matched_required_skills:
            reasons.append('Matched: '+', '.join(fit.score_breakdown.matched_required_skills[:5]))
        if fit.score_breakdown.missing_required_skills:
            reasons.append('To verify: '+', '.join(fit.score_breakdown.missing_required_skills[:5]))
        if fit.score_breakdown.uncertainties:
            reasons.extend(fit.score_breakdown.uncertainties[:2])
        row['match_reasons']=' · '.join(reasons) or fit.eligibility_reason
    rows.sort(key=lambda row:(float(row.get('score') or 0),str(row.get('posted_at') or row.get('created_at') or '')),reverse=True)
    return rows


@app.get('/api/jobs/queue')
def jobs_queue():
    """Returns actionable queue rows, with stale queue entries marked explicitly."""
    settings=jobpilot.load_json('settings.json')
    profile=jobpilot.load_json('profile.json')
    rules=jobpilot.load_json('eligibility.json')
    challenge_waiting=waiting_challenge_for_active_run()
    maximum,_,_=retry_policy(settings)
    rows = query(
        "SELECT j.*, aq.job_id AS job_id, j.status AS job_status, aq.status AS queue_status,"
        " aq.mode, aq.mode AS application_mode, aq.created_at, aq.attempts, aq.next_attempt_at, j.resume AS resume_version, aq.score AS queue_score,"
        " (SELECT COUNT(*) FROM unknown_questions u WHERE u.job_id=j.id AND (u.answer IS NULL OR TRIM(u.answer)='')) AS unanswered_count,"
        " EXISTS(SELECT 1 FROM application_attempts a WHERE a.job_id=j.id AND a.step='submit') AS submitted"
        " FROM application_queue aq"
        " JOIN jobs j ON j.id = aq.job_id"
        " WHERE aq.status NOT IN ('Applied','Skipped')"
        " AND j.status NOT IN ('Applied','Skipped','SkippedBlocked','Rejected')"
        " ORDER BY aq.score DESC LIMIT 200"
    )
    in_progress=set()
    if _active:
        in_progress={row['job_id'] for row in query("SELECT DISTINCT job_id FROM application_attempts WHERE completed_at IS NULL AND status='Running'")}
        run=query('SELECT log_path FROM agent_runs WHERE id=?',(_active,))
        if run:
            try:
                log_text=Path(run[0]['log_path']).read_text(encoding='utf-8',errors='replace')[-20000:]
                opened=list(re.finditer(r'Opening job (\d+):\s*([^\r\n]+)',log_text))
                if opened:in_progress.add(int(opened[-1].group(1)))
            except OSError:
                pass
    for row in rows:
        row['active_now']=row['id'] in in_progress
        row.setdefault('match_reasons', '')
        row['readiness']=submission_readiness(row,
            {'status':row['queue_status'],'attempts':row['attempts'],'next_attempt_at':row['next_attempt_at']},
            bool(row['submitted']),settings,profile,rules,challenge_waiting)
        if (row['job_status']=='ManualReview' and row['queue_status']=='ReviewRequired'):
            row['readiness']={'ready':False,'reason_code':'manual_review','message':'This application needs your review before it can be submitted.'}
        elif row['job_status']!='Queued' and row['readiness']['reason_code']=='job_not_queued':
            row['readiness']={'ready':False,'reason_code':'stale_queue_entry','message':f"Queue says {row['queue_status']}, but tracker says {row['job_status']}. Remove this stale queue entry or reapprove the job."}
        row['can_submit']=row['readiness']['ready']
        row['recovery']=recovery_guidance(row['job_status'],row['eligibility_reason'],row['readiness']['reason_code']=='already_submitted')
        row['recovery']['automatic_retry']=bool(row['recovery']['automatic_retry'] and row['next_attempt_at'] and (row['attempts'] or 0)<maximum)
        row['status']=row['queue_status']
        row['score']=row['queue_score']
    attach_application_states(rows)
    return rows


@app.post('/api/jobs/{job_id}/approve')
def approve_job(job_id: int):
    if _active:raise HTTPException(409,'Stop the worker before changing the queue')
    db=jobpilot.connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        job=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job:raise HTTPException(404,'Job not found')
        if job['status'] in ('Applied','Rejected') or db.execute("SELECT 1 FROM application_attempts WHERE job_id=? AND (step='submit' OR completed_at IS NULL)",(job_id,)).fetchone():
            raise HTTPException(409,'This application cannot be requeued; review its submission evidence')
        ok,reason=jobpilot.eligible(job,jobpilot.load_json('eligibility.json'))
        if not ok:raise HTTPException(409,reason)
        queue=db.execute('SELECT attempts,next_attempt_at FROM application_queue WHERE job_id=?',(job_id,)).fetchone()
        maximum,_,_=retry_policy(jobpilot.load_json('settings.json'))
        if queue and (queue['attempts']>=maximum or not jobpilot.due(queue['next_attempt_at'],jobpilot.now())):
            raise HTTPException(409,'Attempt limit reached or retry delay has not elapsed')
        db.execute("INSERT INTO application_queue(job_id,priority,score,status,created_at) VALUES(?,?,?,'Queued',?) ON CONFLICT(job_id) DO UPDATE SET status='Queued',error=NULL,next_attempt_at=NULL",
                   (job_id,job['priority'],job['score'],jobpilot.now()))
        db.execute("UPDATE jobs SET status='Queued',eligibility_reason='Approved for application' WHERE id=?",(job_id,))
        db.commit()
    finally:db.close()
    return {'approved':True,'job_id':job_id}


class ManualAppliedInput(BaseModel):
    confirmation_text:str=Field(min_length=3,max_length=2000)


@app.post('/api/jobs/{job_id}/manual-applied')
def record_manual_application(job_id:int,payload:ManualAppliedInput):
    """Record a user's employer confirmation without claiming browser verification."""
    if job_id<1 or not payload.confirmation_text.strip():raise HTTPException(422,'Enter the employer confirmation text or reference')
    with _guard:
        if _active:raise HTTPException(409,'Finish the active worker before recording a manual application')
        try:
            with exclusive_lock(jobpilot.DATA/'browser-application.lock'):
                db=jobpilot.connect()
                try:
                    db.execute('BEGIN IMMEDIATE')
                    job=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
                    if not job:raise HTTPException(404,'Job not found')
                    if job['status'] in ('Applied','Rejected'):
                        raise HTTPException(409,'This job is already applied or was rejected')
                    timestamp=jobpilot.now()
                    previous=db.execute("SELECT * FROM application_attempts WHERE job_id=? AND step='submit' ORDER BY id DESC LIMIT 1",(job_id,)).fetchone()
                    if previous:
                        attempt_id=previous['id']
                        mode='USER_CONFIRMED'
                    else:
                        attempt_id=db.execute("INSERT INTO application_attempts(job_id,started_at,completed_at,status,adapter,step) VALUES(?,?,?,'Applied',?,'submit')",
                                              (job_id,timestamp,timestamp,job['ats'])).lastrowid
                        mode='MANUAL'
                    directory=jobpilot.DATA/'screenshots'/f'job-{job_id}'/f'attempt-{attempt_id}'
                    receipt_path=directory/'receipt.json'
                    if receipt_path.exists():raise HTTPException(409,'A receipt already exists for this attempt')
                    atomic_json(receipt_path,{'job_id':job_id,'attempt_id':attempt_id,'mode':mode,
                        'verification':'user_reported','confirmation_text':payload.confirmation_text.strip(),
                        'employer_url':job['apply_url'],'selected_resume':job['resume'],'recorded_at':timestamp})
                    attempt_path=directory/'attempt.json'
                    try:metadata=json.loads(attempt_path.read_text(encoding='utf-8')) if attempt_path.is_file() else {}
                    except (OSError,ValueError):metadata={}
                    metadata.update(job_id=job_id,attempt_id=attempt_id,status='Applied',manual_confirmation_at=timestamp)
                    atomic_json(attempt_path,metadata)
                    db.execute("UPDATE application_attempts SET status='Applied',completed_at=?,error=NULL WHERE id=?",(timestamp,attempt_id))
                    db.execute("UPDATE jobs SET status='Applied',eligibility_reason='Applied manually; confirmation recorded by user' WHERE id=?",(job_id,))
                    db.execute("INSERT INTO application_queue(job_id,priority,score,mode,attempts,status,created_at) VALUES(?,?,?, 'MANUAL',1,'Applied',?) ON CONFLICT(job_id) DO UPDATE SET status='Applied',mode=CASE WHEN ? THEN mode || '_USER_CONFIRMED' ELSE 'MANUAL' END,error=NULL,next_attempt_at=NULL",
                               (job_id,job['priority'],job['score'],timestamp,bool(previous)))
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                finally:db.close()
        except WorkerBusy as exc:
            raise HTTPException(409,'Another application attempt is active; finish it before recording a manual result') from exc
    return {'recorded':True,'job_id':job_id,'verification':'user_reported'}


@app.post('/api/jobs/{job_id}/reject')
def reject_job(job_id: int):
    """Skip an unsubmitted queued job and promote the next eligible candidate."""
    with _guard:
        if _active:
            active_runs=query('SELECT log_path FROM agent_runs WHERE id=?',(_active,))
            if active_runs:
                try:
                    recent=Path(active_runs[0]['log_path']).read_text(encoding='utf-8',errors='replace')[-20000:]
                    opened=list(re.finditer(r'Opening job (\d+):\s*([^\r\n]+)',recent))
                    if opened and int(opened[-1].group(1))==job_id:
                        raise HTTPException(409,'This job is already being prepared; wait for it to finish before removing it')
                except OSError:
                    pass
        db=None
        try:
            db=jobpilot.connect()
            db.execute('BEGIN IMMEDIATE')
            job=db.execute('SELECT status FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not job:raise HTTPException(404,'Job not found')
            if job['status']=='Applied':
                incomplete_attempt=db.execute("SELECT 1 FROM application_attempts WHERE job_id=? AND completed_at IS NULL LIMIT 1",(job_id,)).fetchone()
                if incomplete_attempt:
                    raise HTTPException(409,'This application is still active; wait for it to finish before removing it from the queue')
                db.execute('DELETE FROM application_queue WHERE job_id=?',(job_id,))
                db.commit()
                return {'rejected':True,'job_id':job_id,'removed_only':True}
            checkpoint=db.execute("SELECT 1 FROM application_attempts WHERE job_id=? AND (step='submit' OR completed_at IS NULL) LIMIT 1",(job_id,)).fetchone()
            if job['status'] in ('Applied','ManualReview','InProgress','Applying') or checkpoint:
                raise HTTPException(409,'This application may have been submitted or is active; review its evidence before changing it')
            db.execute('DELETE FROM application_queue WHERE job_id=?',(job_id,))
            db.execute("UPDATE jobs SET status='Rejected',eligibility_reason='Removed from queue by user' WHERE id=?",(job_id,))
            replacement=None
            candidates=jobpilot.queue_candidates(db,1)
            if candidates:
                replacement=candidates[0]
                db.execute("INSERT INTO application_queue(job_id,priority,score,status,created_at) VALUES(?,?,?,'Queued',?) ON CONFLICT(job_id) DO UPDATE SET priority=excluded.priority,score=excluded.score,status='Queued',error=NULL,next_attempt_at=NULL",
                           (replacement['id'],replacement['priority'],replacement['score'],jobpilot.now()))
                db.execute("UPDATE jobs SET status='Queued',eligibility_reason='Promoted to replace a removed queue item' WHERE id=?",(replacement['id'],))
            db.commit()
        except HTTPException:
            if db is not None:db.rollback()
            raise
        except Exception as exc:
            if db is not None:db.rollback()
            raise HTTPException(500,'Could not reject this job; no change was reported as successful') from exc
        finally:
            if db is not None:db.close()
    promoted={'job_id':replacement['id'],'company':replacement['company'],'title':replacement['title']} if replacement else None
    return {'rejected': True, 'job_id': job_id,'replacement':promoted}


class ResumeInput(BaseModel):
    resume: str = Field(pattern=r'^(job-specific|backend|fullstack|platform-sre|security|generic)$')


@app.post('/api/jobs/{job_id}/set-resume')
def set_resume(job_id: int, body: ResumeInput):
    if _active:raise HTTPException(409,'Stop the worker before changing the resume')
    db=jobpilot.connect()
    try:
        job=db.execute('SELECT status,title,description FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job:raise HTTPException(404,'Job not found')
        if job['status']=='Applied' or db.execute("SELECT 1 FROM application_attempts WHERE job_id=? AND (step='submit' OR completed_at IS NULL)",(job_id,)).fetchone():
            raise HTTPException(409,'Resume cannot be changed for a submitted or active application')
        if body.resume=='job-specific':
            filename=jobpilot.application_resume_for(job['title']+' '+job['description'],job_id)
        else:
            tailored=jobpilot.DATA/'resumes'/f'tailored-{body.resume}.pdf'
            filename=tailored.name if tailored.is_file() and tailored.stat().st_size>1000 else body.resume+'.pdf'
        if not (jobpilot.DATA/'resumes'/filename).is_file():raise HTTPException(422,'Selected resume PDF is missing')
        db.execute('UPDATE jobs SET resume=? WHERE id=?',(filename,job_id))
        db.commit()
    finally:db.close()
    return {'saved':True,'job_id':job_id,'resume':filename}


@app.get('/api/stats/daily')
def daily_stats():
    """Return calendar-day activity in the candidate's local timezone."""
    from datetime import time as day_start
    from zoneinfo import ZoneInfo
    local_zone=ZoneInfo('Asia/Kolkata')
    end=datetime.now(timezone.utc)
    local_date=end.astimezone(local_zone).date()
    start=datetime.combine(local_date,day_start.min,tzinfo=local_zone).astimezone(timezone.utc)
    start_text=start.isoformat(timespec='seconds')
    end_text=end.isoformat(timespec='seconds')
    discovered=query('SELECT COUNT(*) AS n FROM jobs WHERE discovered_at>=? AND discovered_at<=?',(start_text,end_text))[0]['n']
    qualified=query('SELECT COUNT(*) AS n FROM jobs WHERE discovered_at>=? AND discovered_at<=? AND score>=50',(start_text,end_text))[0]['n']
    applied=query("SELECT COUNT(DISTINCT job_id) AS n FROM application_attempts WHERE status='Applied' AND completed_at>=? AND completed_at<=?",(start_text,end_text))[0]['n']
    failed=query("SELECT COUNT(*) AS n FROM application_attempts WHERE status IN ('FailedTemporary','Failed') AND completed_at>=? AND completed_at<=?",(start_text,end_text))[0]['n']
    portal_rows=query('SELECT DISTINCT ats FROM jobs WHERE discovered_at>=? AND discovered_at<=?',(start_text,end_text))
    portals_active=[row['ats'] for row in portal_rows if row.get('ats')]
    return {
        'jobs_discovered': discovered,
        'jobs_qualified': qualified,
        'jobs_applied': applied,
        'jobs_failed': failed,
        'captcha_challenges': None,
        'captcha_challenges_available': False,
        'portals_active': portals_active,
        'window': 'calendar_day',
        'timezone': 'Asia/Kolkata',
        'window_start': start_text,
        'window_end': end_text,
    }


@app.get('/api/metrics/tokens')
def token_metrics():
    """Return measured usage only; never represent missing telemetry as zero."""
    from datetime import time as day_start
    from zoneinfo import ZoneInfo
    local_zone=ZoneInfo('Asia/Kolkata')
    end=datetime.now(timezone.utc)
    start=datetime.combine(end.astimezone(local_zone).date(),day_start.min,tzinfo=local_zone).astimezone(timezone.utc)
    start_text=start.isoformat(timespec='seconds')
    end_text=end.isoformat(timespec='seconds')
    db=jobpilot.connect()
    try:
        columns={row['name'] for row in db.execute('PRAGMA table_info(agent_runs)')}
        runs_today=db.execute('SELECT COUNT(*) FROM agent_runs WHERE started_at>=? AND started_at<=?',(start_text,end_text)).fetchone()[0]
        if not {'input_tokens','output_tokens'}<=columns:
            return {'available':False,'total_input_tokens':None,'total_output_tokens':None,
                    'estimated_cost_usd':None,'cost_available':False,'runs_today':runs_today,
                    'window':'calendar_day','timezone':'Asia/Kolkata','window_start':start_text,'window_end':end_text}
        row=db.execute('SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0) FROM agent_runs WHERE started_at>=? AND started_at<=?',(start_text,end_text)).fetchone()
    finally:db.close()
    return {
        'available':True,
        'total_input_tokens': row[0],
        'total_output_tokens': row[1],
        'estimated_cost_usd': None,
        'cost_available':False,
        'runs_today': runs_today,
        'window':'calendar_day','timezone':'Asia/Kolkata','window_start':start_text,'window_end':end_text,
    }


class DiaryEntryInput(BaseModel):
    entry_type: str = Field(default='hr_call')
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(default='')
    contact_name: str = Field(default='')
    contact_email: str = Field(default='')
    contact_phone: str = Field(default='')
    summary: str = Field(min_length=1)
    action_items: str = Field(default='')
    status: str = Field(default='pending')
    due_date: str | None = None
    draft_subject: str = Field(default='')
    draft_body: str = Field(default='')


class DiaryEntryUpdate(BaseModel):
    entry_type: str | None = None
    company: str | None = None
    role: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    summary: str | None = None
    action_items: str | None = None
    status: str | None = None
    due_date: str | None = None
    draft_subject: str | None = None
    draft_body: str | None = None


class DraftFollowupInput(BaseModel):
    company: str
    contact_name: str = ''
    role: str = ''
    notes: str = ''
    context: str = 'hr_call'


@app.get('/api/diary/entries')
def diary_entries(entry_type: str | None = None, status: str | None = None, limit: int = 100):
    import email_diary_service
    return email_diary_service.get_diary_entries(entry_type=entry_type, status=status, limit=limit)


@app.post('/api/diary/entries')
def add_diary(payload: DiaryEntryInput):
    import email_diary_service
    entry_id = email_diary_service.add_diary_entry(
        entry_type=payload.entry_type,
        company=payload.company,
        role=payload.role,
        contact_name=payload.contact_name,
        contact_email=payload.contact_email,
        contact_phone=payload.contact_phone,
        summary=payload.summary,
        action_items=payload.action_items,
        status=payload.status,
        due_date=payload.due_date,
        draft_subject=payload.draft_subject,
        draft_body=payload.draft_body
    )
    return {'id': entry_id, 'saved': True}


@app.patch('/api/diary/entries/{entry_id}')
def update_diary(entry_id: int, payload: DiaryEntryUpdate):
    import email_diary_service
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    success = email_diary_service.update_diary_entry(entry_id, **updates)
    if not success:
        raise HTTPException(404, 'Diary entry not found or no updates provided')
    return {'id': entry_id, 'updated': True}


@app.delete('/api/diary/entries/{entry_id}')
def delete_diary(entry_id: int):
    import email_diary_service
    success = email_diary_service.delete_diary_entry(entry_id)
    if not success:
        raise HTTPException(404, 'Diary entry not found')
    return {'id': entry_id, 'deleted': True}


@app.get('/api/diary/metrics')
def diary_metrics():
    import email_diary_service
    return email_diary_service.get_diary_metrics()


@app.post('/api/diary/sync-emails')
def sync_emails(limit: int = 50):
    import email_diary_service
    return email_diary_service.sync_gmail_emails(max_emails=limit)


@app.get('/api/diary/emails')
def synced_emails(category: str | None = None, limit: int = 50):
    sql = "SELECT * FROM synced_emails"
    params = []
    if category:
        sql += " WHERE category = ?"
        params.append(category)
    sql += " ORDER BY synced_at DESC LIMIT ?"
    params.append(limit)
    return query(sql, tuple(params))


@app.post('/api/diary/draft-followup')
def draft_followup(payload: DraftFollowupInput):
    import email_diary_service
    return email_diary_service.generate_followup_draft(
        company=payload.company,
        contact_name=payload.contact_name,
        role=payload.role,
        notes=payload.notes,
        context=payload.context
    )


def serve_dashboard():
    """Own the dashboard process lock before FastAPI startup touches the database."""
    global _server
    import uvicorn
    try:
        with exclusive_lock(jobpilot.DATA/'dashboard-server.lock'):
            _server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8765,access_log=False))
            _server.run()
    except WorkerBusy:
        # A competing launcher must never run initialize() or interrupt the
        # healthy server's database state before losing the port bind.
        print('Another JobPilot dashboard owns the server lock.',flush=True)


if __name__=='__main__':
    serve_dashboard()
