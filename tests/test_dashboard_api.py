"""Dashboard API contract tests using disposable database and configuration files."""
from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_server
import jobpilot


class DashboardApiContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp')
        self.root=Path(self.tmp.name)
        self.config=self.root/'config'
        self.config.mkdir()
        self.profile={'personal':{'firstName':'Test','lastName':'Candidate','email':'test@example.com','phone':'1234'},
                      'preferences':{'roles':['Software Engineer'],'locations':['India']}}
        settings={'allowedAutoSubmitAts':['greenhouse','lever'],'retry':{'maxAttempts':3,'baseDelaySeconds':60,'maxDelaySeconds':3600}}
        for name,value in [('profile.json',self.profile),('answers.json',{}),('eligibility.json',{}),('skills.json',{}),('settings.json',settings)]:
            (self.config/name).write_text(json.dumps(value),encoding='utf-8')
        self.patches=[patch('jobpilot.DATA',self.root),patch('jobpilot.DB',self.root/'test.db'),
                      patch('jobpilot.CONFIG',self.config),patch('agent_server._active',None)]
        for item in self.patches:item.start()
        jobpilot._JSON_CACHE.clear()
        self.client=TestClient(agent_server.app,base_url='http://127.0.0.1:8765',headers={'X-Jobpilot-Request':'local-dashboard'})
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        for item in reversed(self.patches):item.stop()
        jobpilot._JSON_CACHE.clear()
        self.tmp.cleanup()

    def create_job(self,status='Discovered',job_id=999999):
        db=jobpilot.connect()
        db.execute("INSERT INTO jobs(id,company,title,description,location,discovered_at,fingerprint,status,score,priority,ats,apply_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (job_id,'TestCo','Software Engineer','.NET C# developer','Bengaluru, India','2026-10-14T00:00:00Z',f'test-fp-{job_id}',status,80,'P1','lever',f'https://jobs.example/{job_id}'))
        db.commit();db.close()

    def test_jobs_today_and_queue_return_lists(self):
        self.assertEqual(self.client.get('/api/jobs/today').status_code,200)
        self.assertIsInstance(self.client.get('/api/jobs/today').json(),list)
        self.assertEqual(self.client.get('/api/jobs/queue').status_code,200)
        self.assertIsInstance(self.client.get('/api/jobs/queue').json(),list)

    def test_queue_marks_entries_stale_when_job_status_is_no_longer_queued(self):
        self.create_job('SkippedBlocked',job_id=999990)
        db=jobpilot.connect()
        db.execute("INSERT INTO application_queue(job_id,created_at,status) VALUES(999990,'today','Queued')")
        db.commit();db.close()
        response=self.client.get('/api/jobs/queue')
        self.assertEqual(response.status_code,200)
        row=next(item for item in response.json() if item['job_id']==999990)
        self.assertEqual(row['job_status'],'SkippedBlocked')
        self.assertFalse(row['can_submit'])
        self.assertEqual(row['readiness']['reason_code'],'stale_queue_entry')
        self.assertIn('Remove this stale queue entry',row['readiness']['message'])

    def test_manual_review_queue_item_is_not_misreported_as_stale(self):
        self.create_job('ManualReview',job_id=999989)
        db=jobpilot.connect()
        db.execute("INSERT INTO application_queue(job_id,created_at,status) VALUES(999989,'today','ReviewRequired')")
        db.commit();db.close()
        with patch('agent_server.external_worker_is_busy',return_value=False):
            response=self.client.get('/api/jobs/queue')
        self.assertEqual(response.status_code,200)
        row=next(item for item in response.json() if item['job_id']==999989)
        self.assertFalse(row['can_submit'])
        self.assertEqual(row['readiness']['reason_code'],'manual_review')
        self.assertIn('needs your review',row['readiness']['message'])

    def test_pipeline_queue_preview_is_read_only_and_excludes_submit_checkpoints(self):
        self.create_job('Qualified',job_id=999998)
        db=jobpilot.connect()
        db.execute("INSERT INTO application_attempts(job_id,started_at,completed_at,status,step) VALUES(999998,'today','today','ManualReview','submit')")
        db.commit();db.close()
        response=self.client.get('/api/pipeline/preview?step=queue&limit=5')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['queue_count'],0)
        self.assertEqual(agent_server.query('SELECT status FROM jobs WHERE id=999998')[0]['status'],'Qualified')

    def test_apply_preview_and_batch_use_only_reviewed_ready_ids(self):
        for job_id in (999991,999992,999993):self.create_job('Queued',job_id)
        db=jobpilot.connect()
        for job_id in (999991,999992,999993):
            db.execute("INSERT INTO application_queue(job_id,status,score,created_at) VALUES(?,'Queued',80,'today')",(job_id,))
        db.execute("UPDATE jobs SET ats='indeed' WHERE id=999993")
        db.commit();db.close()
        settings={'allowedAutoSubmitAts':['lever'],'unattended':True,'autoSubmit':True,'retry':{'maxAttempts':3}}
        (self.config/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
        resume_dir=jobpilot.DATA/'resumes';resume_dir.mkdir()
        (resume_dir/'generic.pdf').write_bytes(b'%PDF-1.4\n%%EOF')
        preview=self.client.get('/api/apply/preview?limit=3')
        self.assertEqual(preview.status_code,200)
        ids=[item['job_id'] for item in preview.json()['items']]
        self.assertEqual(ids,[999991,999992])
        self.assertEqual(preview.json()['items'][0]['location'],'Bengaluru, India')
        self.assertEqual(self.client.post('/api/runs',json={'action':'apply','dry_run':False,'limit':2}).status_code,422)
        with patch('agent_server.threading.Thread') as thread:
            response=self.client.post('/api/runs',json={'action':'apply','dry_run':False,'limit':2,'job_ids':ids})
        self.assertEqual(response.status_code,200)
        command=thread.call_args.kwargs['args'][1]
        self.assertEqual(command[command.index('--jobs')+1],'999991,999992')

    def test_batch_rechecks_each_selected_job_before_worker_launch(self):
        self.create_job('Queued',999990)
        db=jobpilot.connect()
        db.execute("INSERT INTO application_queue(job_id,status,created_at) VALUES(999990,'Queued','today')")
        db.commit();db.close()
        settings={'allowedAutoSubmitAts':['lever'],'unattended':True,'autoSubmit':True}
        (self.config/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
        (self.root/'resumes').mkdir();(self.root/'resumes'/'generic.pdf').write_bytes(b'%PDF-1.4\n%%EOF')
        with patch('agent_server.threading.Thread') as thread:
            response=self.client.post('/api/runs',json={'action':'apply','dry_run':False,'limit':1,'job_ids':[999990,999990]})
            self.assertEqual(response.status_code,422)
            db=jobpilot.connect();db.execute("UPDATE jobs SET ats='indeed' WHERE id=999990");db.commit();db.close()
            response=self.client.post('/api/runs',json={'action':'apply','dry_run':False,'limit':1,'job_ids':[999990]})
        self.assertEqual(response.status_code,409)
        thread.assert_not_called()

    def test_manual_application_saves_user_reported_receipt_and_checkpoint(self):
        self.create_job('Queued',999989)
        db=jobpilot.connect()
        db.execute("UPDATE jobs SET ats='indeed' WHERE id=999989")
        db.execute("INSERT INTO application_queue(job_id,status,created_at) VALUES(999989,'Queued','today')")
        db.commit();db.close()
        self.assertEqual(self.client.post('/api/jobs/999989/manual-applied',json={'confirmation_text':' '}).status_code,422)
        response=self.client.post('/api/jobs/999989/manual-applied',json={'confirmation_text':'Application received #ABC123'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['verification'],'user_reported')
        receipt=self.client.get('/api/evidence/999989/receipt').json()
        self.assertEqual(receipt['confirmation_text'],'Application received #ABC123')
        self.assertEqual(receipt['verification'],'user_reported')
        listed=next(row for row in self.client.get('/api/jobs?hide_applied=false').json() if row['id']==999989)
        self.assertEqual((listed['status'],listed['application_mode']),('Applied','MANUAL'))
        self.assertEqual(self.client.post('/api/jobs/999989/manual-applied',json={'confirmation_text':'Duplicate receipt'}).status_code,409)
        db=jobpilot.connect()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM application_attempts WHERE job_id=999989 AND step='submit'").fetchone()[0],1)
        db.close()
        jobpilot.cmd_export(Namespace())
        with (self.root/'exports'/'JobApplications.csv').open(encoding='utf-8-sig',newline='') as file:
            exported=next(row for row in csv.DictReader(file) if row['JobId']=='999989')
        self.assertEqual(exported['Mode'],'MANUAL')
        self.assertTrue(exported['DateApplied'])

    def test_manual_confirmation_resolves_existing_ambiguous_submit(self):
        self.create_job('ManualReview',999988)
        db=jobpilot.connect()
        db.execute("INSERT INTO application_queue(job_id,status,mode,created_at) VALUES(999988,'ReviewRequired','AUTO','today')")
        db.execute("INSERT INTO application_attempts(job_id,started_at,completed_at,status,adapter,step) VALUES(999988,'today','today','ManualReview','lever','submit')")
        db.commit();db.close()
        response=self.client.post('/api/jobs/999988/manual-applied',json={'confirmation_text':'Employer confirmation #98765'})
        self.assertEqual(response.status_code,200)
        db=jobpilot.connect()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM application_attempts WHERE job_id=999988").fetchone()[0],1)
        self.assertEqual(db.execute("SELECT mode FROM application_queue WHERE job_id=999988").fetchone()[0],'AUTO_USER_CONFIRMED')
        db.close()

    def test_pipeline_preview_rejects_unknown_step(self):
        self.assertEqual(self.client.get('/api/pipeline/preview?step=apply').status_code,422)

    def test_dashboard_no_longer_accepts_implicit_full_pipeline_run(self):
        self.assertEqual(self.client.post('/api/runs',json={'action':'run'}).status_code,422)

    def test_unvalidated_adapter_is_blocked_before_worker_launch(self):
        self.create_job('Queued',job_id=999997)
        db=jobpilot.connect();db.execute("UPDATE jobs SET ats='indeed' WHERE id=999997");db.execute("INSERT INTO application_queue(job_id,status,created_at) VALUES(999997,'Queued','today')");db.commit();db.close()
        listed=next(row for row in self.client.get('/api/jobs?hide_applied=false').json() if row['id']==999997)
        self.assertFalse(listed['can_submit'])
        self.assertEqual(listed['readiness']['reason_code'],'unsupported_adapter')
        response=self.client.post('/api/runs',json={'action':'apply','job':999997,'dry_run':False})
        self.assertEqual(response.status_code,409)
        self.assertIn('no validated submit-and-confirm adapter',response.json()['detail'])
        self.assertIsNone(agent_server._active)

    def test_non_application_worker_does_not_false_block_a_ready_submission(self):
        self.create_job('Queued',job_id=999994)
        db=jobpilot.connect()
        db.execute("INSERT INTO application_queue(job_id,status,created_at) VALUES(999994,'Queued','today')")
        db.commit();db.close()
        settings={'allowedAutoSubmitAts':['lever'],'unattended':True,'autoSubmit':True,'retry':{'maxAttempts':3}}
        (self.config/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
        (self.root/'resumes').mkdir();(self.root/'resumes'/'generic.pdf').write_bytes(b'%PDF-1.4\\n%%EOF')
        db=jobpilot.connect()
        db.execute("INSERT INTO agent_runs(id,action,status,started_at) VALUES('score-test','score','Running','today')")
        db.commit();db.close()
        agent_server._active='score-test'
        try:
            listed=next(row for row in self.client.get('/api/jobs?hide_applied=false').json() if row['id']==999994)
            self.assertTrue(listed['can_submit'])
            self.assertEqual(listed['readiness']['reason_code'],'ready')
        finally:
            agent_server._active=None

    def test_apply_preflight_detects_external_cli_worker(self):
        self.create_job('Queued',job_id=999995)
        db=jobpilot.connect();db.execute("INSERT INTO application_queue(job_id,status,created_at) VALUES(999995,'Queued','today')");db.commit();db.close()
        (self.config/'settings.json').write_text(json.dumps({'allowedAutoSubmitAts':['lever'],'unattended':True,'autoSubmit':True,'retry':{'maxAttempts':3}}),encoding='utf-8')
        jobpilot._JSON_CACHE.clear()
        with patch('agent_server.external_worker_is_busy',return_value=True), patch('agent_server.threading.Thread') as thread:
            response=self.client.post('/api/runs',json={'action':'apply','job':999995,'dry_run':False})
        self.assertEqual(response.status_code,409)
        self.assertIn('command-line task is already running',response.json()['detail'])
        thread.assert_not_called()
        self.assertIsNone(agent_server._active)

    def test_apply_preflight_requires_ready_profile_resume_and_verified_adapter(self):
        self.create_job('Queued',job_id=999996)
        db=jobpilot.connect();db.execute("INSERT INTO application_queue(job_id,status,created_at) VALUES(999996,'Queued','today')");db.commit();db.close()
        settings={'allowedAutoSubmitAts':['lever'],'unattended':True,'autoSubmit':True,'retry':{'maxAttempts':3}}
        (self.config/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
        resume_dir=jobpilot.DATA/'resumes';resume_dir.mkdir()
        (resume_dir/'generic.pdf').write_bytes(b'%PDF-1.4\n%%EOF')
        with patch('agent_server.threading.Thread'):
            response=self.client.post('/api/runs',json={'action':'apply','job':999996,'dry_run':False})
        self.assertEqual(response.status_code,200)
        self.assertIsNotNone(agent_server._active)

    def test_approve_job_uses_disposable_database(self):
        self.create_job()
        response=self.client.post('/api/jobs/999999/approve')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'approved':True,'job_id':999999})

    def test_reject_job_updates_job_and_queue_consistently(self):
        self.create_job('Queued')
        db=jobpilot.connect()
        db.execute("INSERT INTO application_queue(job_id,status,created_at) VALUES(999999,'Queued','today')")
        db.commit();db.close()
        response=self.client.post('/api/jobs/999999/reject')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'rejected':True,'job_id':999999})
        db=jobpilot.connect()
        self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=999999').fetchone()['status'],'Rejected')
        self.assertIsNone(db.execute('SELECT 1 FROM application_queue WHERE job_id=999999').fetchone())
        db.close()

    def test_reject_requires_existing_job(self):
        self.assertEqual(self.client.post('/api/jobs/999999/reject').status_code,404)

    def test_reject_preserves_applied_review_and_active_jobs(self):
        for status in ('Applied','ManualReview','InProgress','Applying'):
            with self.subTest(status=status):
                self.create_job(status)
                response=self.client.post('/api/jobs/999999/reject')
                self.assertEqual(response.status_code,409)
                db=jobpilot.connect()
                self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=999999').fetchone()['status'],status)
                db.close()
                db=jobpilot.connect();db.execute('DELETE FROM jobs WHERE id=999999');db.commit();db.close()

    def test_reject_refuses_submit_checkpoint(self):
        self.create_job('FailedTemporary')
        db=jobpilot.connect()
        db.execute("INSERT INTO application_attempts(job_id,started_at,completed_at,step) VALUES(999999,'today','today','submit')")
        db.commit();db.close()
        self.assertEqual(self.client.post('/api/jobs/999999/reject').status_code,409)

    def test_reject_does_not_hide_database_failures(self):
        self.create_job()
        with patch('jobpilot.connect',side_effect=sqlite3.OperationalError('fixture database failure')):
            response=self.client.post('/api/jobs/999999/reject')
        self.assertEqual(response.status_code,500)
        self.assertTrue(response.json()['detail'])

    def test_reject_refuses_while_worker_active(self):
        self.create_job()
        agent_server._active='fixture-run'
        try:self.assertEqual(self.client.post('/api/jobs/999999/reject').status_code,409)
        finally:agent_server._active=None

    def test_concurrent_saved_answers_are_merged_under_shared_config_lock(self):
        def save(question,answer):
            jobpilot.cmd_remember_answer(Namespace(question=question,answer=answer))
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda item:save(*item),[('Question one?','Answer one'),('Question two?','Answer two')]))
        answers=json.loads((self.config/'answers.json').read_text(encoding='utf-8'))
        self.assertEqual(answers['exact_questions'][jobpilot.norm('Question one?')],'Answer one')
        self.assertEqual(answers['exact_questions'][jobpilot.norm('Question two?')],'Answer two')

    def test_stale_profile_save_cannot_overwrite_new_question_answer(self):
        create=self.client.get('/api/profile').json()
        db=jobpilot.connect()
        db.execute("INSERT INTO unknown_questions(id,normalized_question,question,created_at) VALUES(1,'preferred location','Preferred location','today')")
        db.commit();db.close()
        self.assertEqual(self.client.post('/api/questions/1',json={'answer':'Hyderabad'}).status_code,200)
        stale=self.client.put('/api/profile',json={'profile':create['profile'],'answers':{'old_key':'old'},'revision':create['revision']})
        self.assertEqual(stale.status_code,409)
        answers=self.client.get('/api/profile').json()['answers']
        self.assertEqual(answers['exact_questions'][jobpilot.norm('Preferred location')],'Hyderabad')

    def test_supervisor_releases_active_worker_when_recovery_fails(self):
        run_id='fixture-supervisor'
        run_dir=self.root/'agent-runs';run_dir.mkdir()
        log_path=run_dir/'fixture.log'
        db=jobpilot.connect()
        db.execute("INSERT INTO agent_runs(id,action,status,started_at,log_path) VALUES(?,'test','Running','today',?)",(run_id,str(log_path)))
        db.commit();db.close()
        class FinishedProcess:
            def wait(self):return 0
        agent_server._active=run_id
        with patch('agent_server.subprocess.Popen',return_value=FinishedProcess()), \
             patch('agent_server.recover_idle_attempts',side_effect=RuntimeError('fixture recovery failure')):
            agent_server.supervise(run_id,['fixture'],log_path,run_dir/'fixture.stop')
        self.assertIsNone(agent_server._active)
        db=jobpilot.connect()
        self.assertEqual(db.execute('SELECT status FROM agent_runs WHERE id=?',(run_id,)).fetchone()['status'],'Failed')
        db.close()
        self.assertIn('fixture recovery failure',log_path.read_text(encoding='utf-8'))

    def test_continue_requires_the_current_waiting_challenge(self):
        self.assertEqual(self.client.post('/api/continue',json={'challenge_id':'old'}).status_code,409)
        agent_server._active='fixture-run'
        self.assertEqual(self.client.post('/api/continue',json={'challenge_id':'old'}).status_code,409)
        challenge={'challenge_id':'current','run_id':'fixture-run','job_id':12,'attempt_id':3,'company':'Fixture','phase':'before_submit','status':'WaitingForUser'}
        (self.root/'captcha_challenge.json').write_text(json.dumps(challenge),encoding='utf-8')
        self.assertEqual(self.client.post('/api/continue',json={'challenge_id':'old'}).status_code,409)
        self.assertEqual(self.client.post('/api/continue',json={'challenge_id':'current'}).status_code,200)
        self.assertEqual((self.root/'agent-runs'/'fixture-run.continue').read_text(encoding='utf-8'),'current')

    def test_set_resume_valid_value(self):
        self.create_job('Queued')
        folder=jobpilot.DATA/'resumes';folder.mkdir()
        (folder/'backend.pdf').write_bytes(b'%PDF-1.4\n%%EOF')
        response=self.client.post('/api/jobs/999999/set-resume',json={'resume':'backend'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['resume'],'backend.pdf')

    def test_set_resume_invalid_value_fails_validation(self):
        self.assertEqual(self.client.post('/api/jobs/999999/set-resume',json={'resume':'invalid_type'}).status_code,422)

    def test_daily_stats_returns_required_keys(self):
        response=self.client.get('/api/stats/daily')
        self.assertEqual(response.status_code,200)
        self.assertTrue({'jobs_discovered','jobs_qualified','jobs_applied','jobs_failed','captcha_challenges','portals_active'} <= response.json().keys())
        self.assertEqual(response.json()['timezone'],'Asia/Kolkata')
        self.assertIsNone(response.json()['captcha_challenges'])
        self.assertFalse(response.json()['captcha_challenges_available'])

    def test_daily_applied_count_uses_confirmation_event_time(self):
        old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat(timespec='seconds')
        recent=datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.create_job('Applied',999999)
        self.create_job('Applied',999998)
        db=jobpilot.connect()
        db.execute('UPDATE jobs SET discovered_at=? WHERE id=999999',(old,))
        db.execute("INSERT INTO application_attempts(job_id,started_at,completed_at,status,step) VALUES(999999,?,?,'Applied','submit')",(old,recent))
        db.execute('UPDATE jobs SET discovered_at=? WHERE id=999998',(recent,))
        db.execute("INSERT INTO application_attempts(job_id,started_at,completed_at,status,step) VALUES(999998,?,?, 'Applied','submit')",(old,old))
        db.commit();db.close()
        stats=self.client.get('/api/stats/daily').json()
        self.assertEqual(stats['jobs_applied'],1)


    def test_token_metrics_returns_required_keys(self):
        response=self.client.get('/api/metrics/tokens')
        self.assertEqual(response.status_code,200)
        self.assertTrue({'total_input_tokens','total_output_tokens','estimated_cost_usd','runs_today'} <= response.json().keys())
        self.assertFalse(response.json()['available'])
        self.assertIsNone(response.json()['total_input_tokens'])
        self.assertIsNone(response.json()['estimated_cost_usd'])
