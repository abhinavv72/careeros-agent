"""A separate process owns browser automation; the API never runs Playwright itself."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import jobpilot
from agent_runtime import exclusive_lock


def stopped():
    path = os.environ.get('JOBPILOT_STOP_FILE')
    return bool(path and Path(path).exists())


NAUKRI_TERMS = ['.NET Developer', 'C# Developer', 'ASP.NET Core Developer', 'Dot Net Full Stack Developer',
                'Backend Developer', 'Software Engineer', 'Software Developer', 'Full Stack Developer',
                'Angular .NET Developer', 'Azure Developer', 'Application Developer', 'API Developer',
                'Cloud Engineer', 'Platform Engineer']
NAUKRI_LOCATIONS = ['Hyderabad', 'Bengaluru', 'Pune', 'Chennai', 'Noida', 'Gurugram', 'Mumbai', 'India']
NAUKRI_SEARCHES_PER_CYCLE = 8


def next_naukri_searches():
    """Rotate through every role x city pair so each cycle reaches listings the last one did not."""
    import json
    pairs = [(term, city) for term in NAUKRI_TERMS for city in NAUKRI_LOCATIONS]
    state = jobpilot.DATA / 'naukri-search-state.json'
    try:
        offset = int(json.loads(state.read_text(encoding='utf-8')).get('offset', 0)) % len(pairs)
    except (OSError, ValueError, TypeError, AttributeError):
        offset = 0
    chosen = [pairs[(offset + i) % len(pairs)] for i in range(NAUKRI_SEARCHES_PER_CYCLE)]
    try:
        state.write_text(json.dumps({'offset': (offset + NAUKRI_SEARCHES_PER_CYCLE) % len(pairs)}), encoding='utf-8')
    except OSError:
        pass
    return chosen


def process_fresh_jobs(discovered_after, limit):
    """Score, queue, and apply a completed source batch before slower sources run."""
    jobpilot.check_stop()
    jobpilot.cmd_score(argparse.Namespace(discovered_after=discovered_after))
    jobpilot.cmd_queue(argparse.Namespace(limit=limit, discovered_after=discovered_after))
    db=jobpilot.connect()
    try:
        minimum_score=float(jobpilot.load_json('settings.json').get('autoApplyMinimumScore',60))
        fresh_ids=[str(row['id']) for row in db.execute("""SELECT j.id FROM jobs j JOIN application_queue q ON q.job_id=j.id
            WHERE j.status='Queued' AND q.status='Queued' AND j.score>? AND j.discovered_at>=?
            ORDER BY j.score DESC,j.id LIMIT ?""",(minimum_score,discovered_after,limit)).fetchall()]
    finally:
        db.close()
    if fresh_ids:
        # Discovery-triggered submissions are unattended: the pause marker applies.
        os.environ['JOBPILOT_AUTONOMOUS']='1'
        run(argparse.Namespace(action='apply',job=None,jobs=','.join(fresh_ids),dry_run=False,limit=limit))
    else:
        print('No newly discovered job above the auto-apply score threshold is ready for a validated adapter.',flush=True)


def run(args):
    if args.action == 'discover':
        # Refresh imports configured employer boards and third-party job leads,
        # then scores and submits only fresh, high-match jobs through validated adapters.
        discovered_after = jobpilot.now()
        # Process each completed employer feed after its DB commit. This starts
        # scoring and safe application checks as soon as a batch is available,
        # while the remaining employer feeds continue fetching in parallel.
        jobpilot.cmd_discover(argparse.Namespace(
            limit=args.limit,
            on_batch=None if os.environ.get('JOBPILOT_DISCOVERY_ONLY') else lambda batch_started, _company: process_fresh_jobs(batch_started, args.limit),
        ))
        for source_name, discover_source in (
            ('job portals', lambda: __import__('portal_discovery').discover(min(args.limit, 25))),
            ('Remotive', lambda: __import__('remote_discovery').discover(min(args.limit, 200))),
            ('Himalayas', lambda: __import__('himalayas_discovery').discover()),
        ):
            jobpilot.check_stop()
            try:
                source_started=jobpilot.now()
                result = discover_source()
                print(f'{source_name} discovery: {result}', flush=True)
                if not os.environ.get('JOBPILOT_DISCOVERY_ONLY'):
                    process_fresh_jobs(source_started,args.limit)
            except Exception as exc:
                print(f'{source_name} discovery needs attention: {type(exc).__name__}: {exc}', flush=True)
    elif args.action == 'score':
        jobpilot.cmd_score(argparse.Namespace(limit=args.limit))
    elif args.action == 'queue':
        jobpilot.cmd_queue(argparse.Namespace(limit=args.limit))
    elif args.action in ('boards', 'naukri'):
        totals = Counter(succeeded=0, errors=0, fetched=0, imported=0, duplicates=0)
        for term, location in next_naukri_searches():
            jobpilot.check_stop()
            if os.environ.get('JOBPILOT_DISCOVERY_ONLY'):
                jobpilot.wait_for_application_write_window()
            try:
                print(f'Starting Naukri discovery for: {term} in {location} (3 pages)', flush=True)
                result = jobpilot.cmd_naukri_discover(
                    argparse.Namespace(keywords=term, location=location, pages=3, headless=True))
            except (Exception, SystemExit) as exc:
                totals['errors'] += 1
                print(f'Naukri discovery needs attention: {type(exc).__name__}: {exc}', flush=True)
                continue
            totals['succeeded'] += 1
            totals.update(result or {})
        if args.action == 'boards':
            from remote_discovery import discover
            discover(args.limit)
            from himalayas_discovery import discover as discover_himalayas
            discover_himalayas()
        return dict(totals)
    elif args.action == 'remotive':
        from remote_discovery import discover
        return discover(args.limit)
    elif args.action == 'himalayas':
        from himalayas_discovery import discover
        return discover()
    elif args.action == 'portals':
        from portal_discovery import discover
        return discover(args.limit)
    elif args.action == 'custom-link':
        if not args.url:
            raise SystemExit('Provide one HTTPS job application link.')
        job_id,status,reason=jobpilot.import_custom_job(args.url)
        print(f'Custom application link resolved to job {job_id}: {status} — {reason}',flush=True)
    elif args.action == 'login':
        jobpilot.cmd_portal_login(argparse.Namespace(url=args.url))
    elif args.action == 'apply':
        outcomes = Counter()
        selected_ids=[int(value) for value in args.jobs.split(',')] if getattr(args,'jobs',None) else None
        db=jobpilot.connect()
        try:
            jobpilot.promote_due_retries(db)
            if selected_ids:
                rows=[db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone() for job_id in selected_ids]
                missing=[job_id for job_id,row in zip(selected_ids,rows) if row is None]
                for job_id in missing:print(f'Selected job {job_id} is no longer available.',flush=True)
                outcomes["unavailable"] += len(missing)
                rows=[row for row in rows if row is not None]
            elif args.job:
                row=db.execute('SELECT * FROM jobs WHERE id=?',(args.job,)).fetchone()
                rows=[row] if row else []
            else:
                if args.dry_run:
                    rows=db.execute("SELECT * FROM jobs WHERE status='Queued' ORDER BY score DESC LIMIT ?",(args.limit,)).fetchall()
                else:
                    from ats_forms import supports_autosubmit
                    settings=jobpilot.load_json('settings.json')
                    allowed=[ats for ats in settings.get('allowedAutoSubmitAts',[]) if supports_autosubmit(ats)]
                    placeholders=','.join('?' for _ in allowed)
                    minimum_score=float(settings.get('autoApplyMinimumScore',60))
                    rows=db.execute(f"""SELECT j.* FROM jobs j JOIN application_queue q ON q.job_id=j.id
                        WHERE j.status='Queued' AND q.status='Queued' AND (j.score>? OR q.mode='USER_LINK')
                        AND j.ats IN ({placeholders}) ORDER BY (q.mode='USER_LINK') DESC,j.score DESC LIMIT ?""",
                        (minimum_score,*allowed,args.limit)).fetchall() if allowed else []
                if not rows:print('No queued jobs with an enabled submission adapter are ready. Review blocked jobs and unanswered questions.',flush=True)
        finally: db.close()
        print(f'Batch selected: {len(rows)}',flush=True)
        raised_errors = 0
        for position, job in enumerate(rows, start=1):
            if stopped():
                outcomes['unprocessed'] += len(rows) - position + 1
                break
            from application_state import pause_reason
            paused = pause_reason(jobpilot.DATA)
            if paused:
                outcomes['unprocessed'] += len(rows) - position + 1
                print(f'{paused}; remaining queued jobs were left unchanged.', flush=True)
                break
            live_db=jobpilot.connect()
            try:
                current=live_db.execute('SELECT j.status,q.status AS queue_status FROM jobs j LEFT JOIN application_queue q ON q.job_id=j.id WHERE j.id=?',(job['id'],)).fetchone()
            finally:live_db.close()
            if not current or current['status']!='Queued' or current['queue_status']!='Queued':
                outcomes["skipped"] += 1
                print(f"Skipping job {job['id']}: it was removed from the queue before its turn.",flush=True)
                continue
            if not args.dry_run:
                # A job that has already been blocked twice (CAPTCHA, bot check, closed page) will not
                # succeed on a third try; park it instead of tying up the single browser again.
                gate=jobpilot.connect()
                try:
                    blocked_tries=gate.execute("SELECT COUNT(*) FROM application_attempts WHERE job_id=? AND status IN ('SkippedBlocked','FailedTemporary')",(job['id'],)).fetchone()[0]
                    if blocked_tries>=3:
                        gate.execute("UPDATE application_queue SET status='SkippedBlocked',error='Blocked repeatedly; not retried automatically' WHERE job_id=?",(job['id'],))
                        gate.execute("UPDATE jobs SET status='SkippedBlocked' WHERE id=? AND status='Queued'",(job['id'],))
                        gate.commit()
                        outcomes["skipped"] += 1
                        print(f"Job {job['id']} parked: blocked {blocked_tries} times already.",flush=True)
                        continue
                finally:gate.close()
            if not args.dry_run:
                from ats_forms import supports_autosubmit
                enabled={str(ats).lower() for ats in jobpilot.load_json('settings.json').get('allowedAutoSubmitAts',[])}
                if str(job['ats']).lower() not in enabled or not supports_autosubmit(job['ats']):
                    outcomes["skipped"] += 1
                    print(f"Job {job['id']} needs a validated {job['ats']} submission adapter. Use form inspection first.",flush=True)
                    continue
                ok,reason=jobpilot.eligible(job,jobpilot.load_json('eligibility.json'))
                if not ok and not (job['source']=='custom' and not job['company']):
                    blocked_db=jobpilot.connect()
                    try:
                        blocked_db.execute("UPDATE jobs SET status='Skipped',eligible=0,eligibility_reason=? WHERE id=? AND status='Queued'",(reason,job['id']))
                        blocked_db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=? AND status='Queued'",(reason,job['id']))
                        blocked_db.commit()
                    finally:blocked_db.close()
                    outcomes["skipped"] += 1
                    print(f"Job {job['id']} excluded: {reason}",flush=True)
                    continue
            print(f"[{position}/{len(rows)}] Opening job {job['id']}: {job['company']} — {job['title']}",flush=True)
            os.environ['JOBPILOT_CURRENT_JOB']=str(job['id'])
            os.environ['JOBPILOT_CURRENT_COMPANY']=str(job['company'])
            try:
                jobpilot.cmd_apply(argparse.Namespace(job=job['id'],confirm_fill=True,unattended=True,dry_run=args.dry_run,human_challenges=False))
            except KeyboardInterrupt:
                outcomes['unprocessed'] += len(rows) - position
                break
            except (Exception, SystemExit) as exc:
                raised_errors += 1
                print(f"Failed job {job['id']}: {type(exc).__name__}: {exc}", flush=True)
            finally:
                db = jobpilot.connect()
                try:
                    result = db.execute("SELECT status FROM jobs WHERE id=?", (job['id'],)).fetchone()
                finally:
                    db.close()
                status = result['status'] if result else 'unavailable_after_attempt'
                if status == 'Queued':
                    status = 'DryRun' if args.dry_run else 'StillQueued'
                outcomes[status] += 1
        print(f'Application batch outcomes: {dict(outcomes)} (raised errors: {raised_errors})', flush=True)
        jobpilot.cmd_export(args)
    elif args.action == 'retry':
        jobpilot.cmd_retry(argparse.Namespace(all=True, captcha=True))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['discover','score','queue','boards','naukri','remotive','himalayas','portals','custom-link','apply','login','retry'])
    parser.add_argument('--limit',type=int,default=5)
    parser.add_argument('--job',type=int)
    parser.add_argument('--jobs',help='Comma-separated IDs from a reviewed dashboard batch')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--url')
    parser.add_argument('--autonomous',action='store_true',help='Started by the scheduler; honour the dashboard pause marker')
    parsed = parser.parse_args()
    if parsed.autonomous:
        os.environ['JOBPILOT_AUTONOMOUS'] = '1'
    lock_name = 'discovery-worker.lock' if os.environ.get('JOBPILOT_DISCOVERY_ONLY') else 'agent-worker.lock'
    with exclusive_lock(jobpilot.DATA / lock_name):
        run(parsed)
