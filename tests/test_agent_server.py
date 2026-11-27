import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch
import jobpilot


class AgentApiTests(unittest.TestCase):
    def setUp(self):
        import agent_server
        from fastapi.testclient import TestClient
        self.server=agent_server
        self.tmp=tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp')
        self.root=Path(self.tmp.name)
        self.config=self.root/'config';self.config.mkdir()
        self.profile={'personal':{'firstName':'Test','lastName':'Candidate','email':'test@example.com','phone':'1234'}}
        for name,value in [('profile.json',self.profile),('answers.json',{}),('eligibility.json',{}),('companies.json',[])]:
            (self.config/name).write_text(json.dumps(value),encoding='utf-8')
        self.patches=[patch('jobpilot.DATA',self.root),patch('jobpilot.DB',self.root/'test.db'),patch('jobpilot.CONFIG',self.config)]
        for p in self.patches:p.start()
        self.server._active=None
        self.server.initialize()
        self.client=TestClient(self.server.app,base_url='http://127.0.0.1:8765')
        self.headers={'X-Jobpilot-Request':'local-dashboard'}

    def tearDown(self):
        self.client.close();self.server._active=None
        for p in reversed(self.patches):p.stop()
        jobpilot._JSON_CACHE.clear()
        self.tmp.cleanup()

    def test_foreign_origin_and_host_rejected(self):
        self.assertEqual(self.client.get('/api/state',headers={'Origin':'https://example.com'}).status_code,403)
        self.assertEqual(self.client.get('/api/state',headers={'Host':'example.com'}).status_code,403)
        self.assertEqual(self.client.post('/api/stop').status_code,403)

    def test_origin_must_match_local_scheme_and_port(self):
        self.assertEqual(self.client.get('/api/state',headers={'Origin':'http://127.0.0.1:8765'}).status_code,200)
        for origin in ('http://localhost:9000','https://localhost:8765','http://localhost:bad','null'):
            self.assertEqual(self.client.get('/api/state',headers={'Origin':origin}).status_code,403)

    def test_invalid_job_identifier_is_rejected(self):
        self.assertEqual(self.client.post('/api/runs',headers=self.headers,json={'action':'apply','job':-1}).status_code,422)

    def test_thread_launch_failure_does_not_strand_active_run(self):
        with patch('agent_server.threading.Thread') as thread:
            thread.return_value.start.side_effect=RuntimeError('Fixture launch failure')
            reply=self.client.post('/api/runs',headers=self.headers,json={'action':'discover'})
        self.assertEqual(reply.status_code,500)
        self.assertIsNone(self.server._active)
        self.assertEqual(self.server.query('SELECT status FROM agent_runs')[0]['status'],'Failed')

    def test_asgi_startup_initializes_database(self):
        from fastapi.testclient import TestClient
        with TestClient(self.server.app,base_url='http://127.0.0.1:8765') as client:
            self.assertEqual(client.get('/api/state').status_code,200)

    def test_dashboard_singleton_lock_refuses_second_server_before_its_startup(self):
        from agent_runtime import WorkerBusy
        with patch('agent_server.exclusive_lock',side_effect=WorkerBusy('already owned')), \
             patch('uvicorn.Server') as server:
            self.server.serve_dashboard()
        server.assert_not_called()

    def test_startup_does_not_reconcile_run_rows_while_worker_lock_is_held(self):
        from agent_runtime import WorkerBusy
        with patch('agent_server.exclusive_lock',side_effect=WorkerBusy('worker active')), \
             patch('email_diary_service.init_diary_db') as init_diary, \
             patch('jobpilot.connect') as connect:
            self.server.initialize()
        init_diary.assert_not_called()
        connect.assert_not_called()

    def test_health_exposes_loaded_backend_and_detects_changed_code(self):
        health=self.client.get('/api/health').json()
        self.assertEqual(health['service'],'jobpilot')
        self.assertFalse(health['restart_required'])
        with patch('agent_server.backend_build',return_value='changed'):
            self.assertTrue(self.client.get('/api/health').json()['restart_required'])
            self.assertEqual(self.client.post('/api/runs',headers=self.headers,json={'action':'discover'}).status_code,409)

    def test_shutdown_refuses_to_interrupt_a_live_worker(self):
        self.server._active='fixture-live-run'
        self.assertEqual(self.client.post('/api/shutdown',headers=self.headers,json={}).status_code,409)

    def test_profile_persistence_and_validation(self):
        self.assertEqual(self.client.put('/api/profile',headers=self.headers,json={'profile':{},'answers':{}}).status_code,422)
        reply=self.client.put('/api/profile',headers=self.headers,json={'profile':self.profile,'answers':{'years_experience':'3'}})
        self.assertEqual(reply.status_code,200)
        self.assertEqual(self.client.get('/api/profile').json()['answers']['years_experience'],'3')

    def test_single_worker_stop_and_continue(self):
        with patch('agent_server.threading.Thread'):
            response=self.client.post('/api/runs',headers=self.headers,json={'action':'apply','dry_run':True,'limit':1})
            self.assertEqual(response.status_code,200)
            self.assertEqual(self.client.post('/api/runs',headers=self.headers,json={'action':'discover'}).status_code,409)
            challenge={'challenge_id':'run-attempt-one','run_id':response.json()['id'],'job_id':3,'attempt_id':7,'company':'Fixture','phase':'before_submit','status':'WaitingForUser'}
            (self.root/'captcha_challenge.json').write_text(json.dumps(challenge),encoding='utf-8')
            self.assertEqual(self.client.post('/api/continue',headers=self.headers,json={'challenge_id':'stale'}).status_code,409)
            self.assertEqual(self.client.post('/api/continue',headers=self.headers,json={'challenge_id':challenge['challenge_id']}).status_code,200)
            self.assertEqual((self.root/'agent-runs'/f"{response.json()['id']}.continue").read_text(),challenge['challenge_id'])
            self.assertEqual(self.client.post('/api/stop',headers=self.headers,json={}).status_code,200)
            # The pause marker must follow the patched data folder, not the live agent's.
            self.assertTrue((self.root/'autonomy-paused').exists())
            with patch.dict('os.environ',{'JOBPILOT_STOP_FILE':str(self.root/'agent-runs'/f"{response.json()['id']}.stop")}):
                with self.assertRaises(KeyboardInterrupt):jobpilot.check_stop()

    def test_answer_memory_updates_question(self):
        db=jobpilot.connect();db.execute("INSERT INTO unknown_questions(id,normalized_question,question,created_at) VALUES(1,'custom question','Custom question','today')");db.commit();db.close()
        response=self.client.post('/api/questions/1',headers=self.headers,json={'answer':'Saved fact'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(jobpilot.question_answer('Custom question')[0],'Saved fact')
        self.assertEqual(self.client.get('/api/questions').json(),[])

    def test_invalid_actions_and_evidence_paths(self):
        self.assertEqual(self.client.post('/api/runs',headers=self.headers,json={'action':'shell'}).status_code,422)
        self.assertEqual(self.client.post('/api/runs',headers=self.headers,json={'action':'login','url':'file:///etc/passwd'}).status_code,422)
        self.assertEqual(self.client.get('/api/evidence/1/passwords').status_code,404)

    def test_attempt_evidence_is_scoped_to_job_and_attempt(self):
        db=jobpilot.connect()
        db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status) VALUES(1,'Fixture','Engineer','today','unique','Queued')")
        for attempt in (1,2):
            folder=self.root/'screenshots'/'job-1'/f'attempt-{attempt}';folder.mkdir(parents=True)
            (folder/'receipt.json').write_text(json.dumps({'attempt_id':attempt}))
            db.execute("INSERT INTO application_attempts(id,job_id,started_at,completed_at,status,screenshot_path) VALUES(?,1,'today','today','DryRun',?)",(attempt,str(folder/'filled.png')))
        db.commit();db.close()
        self.assertEqual(self.client.get('/api/evidence/1/receipt?attempt_id=1').json()['attempt_id'],1)
        self.assertEqual(self.client.get('/api/evidence/1/receipt').json()['attempt_id'],2)
        self.assertEqual(self.client.get('/api/evidence/2/receipt?attempt_id=1').status_code,404)
        rows=self.client.get('/api/jobs/1/attempts').json()
        self.assertEqual(len(rows),2)
        self.assertIn('receipt',rows[0]['evidence'])

    def test_missing_latest_receipt_does_not_return_old_receipt(self):
        db=jobpilot.connect()
        db.execute("INSERT INTO application_attempts(id,job_id,started_at,completed_at,screenshot_path) VALUES(1,1,'today','today','attempt-1/filled.png')")
        db.commit();db.close()
        folder=self.root/'screenshots';folder.mkdir()
        (folder/'job-1-receipt.json').write_text('{"stale":true}')
        self.assertEqual(self.client.get('/api/evidence/1/receipt').status_code,404)

    def test_resume_selection_saves_pdf_filename_and_preserves_mode(self):
        folder=self.root/'resumes';folder.mkdir();(folder/'backend.pdf').write_bytes(b'%PDF-1.4\n%%EOF')
        db=jobpilot.connect()
        db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status) VALUES(1,'Fixture','Engineer','today','unique','Queued')")
        db.execute("INSERT INTO application_queue(job_id,created_at,mode) VALUES(1,'today','AUTO')")
        db.commit();db.close()
        reply=self.client.post('/api/jobs/1/set-resume',headers=self.headers,json={'resume':'backend'})
        self.assertEqual(reply.status_code,200)
        self.assertEqual(self.server.query('SELECT resume FROM jobs')[0]['resume'],'backend.pdf')
        self.assertEqual(self.server.query('SELECT mode FROM application_queue')[0]['mode'],'AUTO')

    def test_approval_cannot_requeue_a_submission_checkpoint(self):
        db=jobpilot.connect()
        db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status) VALUES(1,'Fixture','Engineer','today','unique','ManualReview')")
        db.execute("INSERT INTO application_attempts(job_id,started_at,completed_at,step) VALUES(1,'today','today','submit')")
        db.commit();db.close()
        reply=self.client.post('/api/jobs/1/approve',headers=self.headers,json={})
        self.assertEqual(reply.status_code,409)
        self.assertEqual(self.server.query('SELECT status FROM jobs')[0]['status'],'ManualReview')

    def test_skipping_queue_job_promotes_next_matching_candidate(self):
        db=jobpilot.connect()
        db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status) VALUES(1,'SkipCo','Engineer','today','skip-unique','Queued')")
        db.execute("INSERT INTO application_queue(job_id,created_at,mode,status,score) VALUES(1,'today','AUTO','Queued',80)")
        db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,score,priority) VALUES(2,'NextCo','Backend Engineer','today','next-unique','Qualified',75,1)")
        db.commit();db.close()
        candidate={'id':2,'company':'NextCo','title':'Backend Engineer','score':75,'priority':1}
        with patch('jobpilot.queue_candidates',return_value=[candidate]):
            reply=self.client.post('/api/jobs/1/reject',headers=self.headers)
        self.assertEqual(reply.status_code,200,reply.text)
        self.assertEqual(reply.json()['replacement']['job_id'],2)
        self.assertEqual(self.server.query('SELECT status FROM jobs WHERE id=1')[0]['status'],'Rejected')
        self.assertEqual(self.server.query('SELECT status FROM jobs WHERE id=2')[0]['status'],'Queued')
        self.assertEqual(self.server.query('SELECT job_id FROM application_queue ORDER BY job_id'),[{'job_id':2}])

    def test_queue_job_with_submission_checkpoint_cannot_be_removed(self):
        db=jobpilot.connect()
        db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status) VALUES(1,'Fixture','Engineer','today','checkpoint-unique','Queued')")
        db.execute("INSERT INTO application_queue(job_id,created_at,mode,status) VALUES(1,'today','AUTO','Queued')")
        db.execute("INSERT INTO application_attempts(job_id,started_at,step) VALUES(1,'today','submit')")
        db.commit();db.close()
        reply=self.client.post('/api/jobs/1/reject',headers=self.headers)
        self.assertEqual(reply.status_code,409)
        self.assertEqual(self.server.query('SELECT status FROM jobs WHERE id=1')[0]['status'],'Queued')

    def test_custom_link_requires_public_https(self):
        for url in ('http://example.com/job','https://localhost/job','https://user:pass@example.com/job'):
            reply=self.client.post('/api/runs',headers=self.headers,json={'action':'custom-link','url':url})
            self.assertEqual(reply.status_code,422)

    def test_verification_is_validated_and_requires_active_worker(self):
        self.assertEqual(self.client.post('/api/verification',headers=self.headers,json={'code':'ABCDEF12'}).status_code,409)
        self.server._active='verification-test'
        (self.root/'agent-runs').mkdir()
        self.assertEqual(self.client.post('/api/verification',headers=self.headers,json={'code':'<invalid>'}).status_code,422)
        self.assertEqual(self.client.post('/api/verification',headers=self.headers,json={'code':'ABCDEF12'}).status_code,200)
        self.assertTrue((self.root/'agent-runs'/'verification-test.code').exists())

    def test_real_offline_worker_process_records_completion(self):
        run_id='offline-worker'
        run_dir=self.root/'agent-runs';run_dir.mkdir()
        path=run_dir/'offline.log'
        db=jobpilot.connect();db.execute("INSERT INTO agent_runs(id,action,status,started_at,log_path) VALUES(?, 'test', 'Running','today',?)",(run_id,str(path)));db.commit();db.close()
        self.server._active=run_id
        self.server.supervise(run_id,[sys.executable,'-c',"print('Offline worker completed')"],path,run_dir/'offline.stop')
        self.assertIn('Offline worker completed',path.read_text())
        self.assertEqual(self.server.query('SELECT status FROM agent_runs')[0]['status'],'Completed')
        self.assertIsNone(self.server._active)

    def test_chat_extracts_ten_job_links_and_starts_apply(self):
        settings={'unattended':True,'autoSubmit':True,'allowedAutoSubmitAts':['greenhouse','lever','ashby'],'autoApplyMinimumScore':60}
        (self.config/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
        jobpilot._JSON_CACHE.clear()
        links=[
            'https://boards.greenhouse.io/acme/jobs/101',
            'https://boards.greenhouse.io/acme/jobs/102',
            'https://jobs.lever.co/beta/201',
            'https://jobs.lever.co/beta/202',
            'https://jobs.ashbyhq.com/gamma/301',
            'https://jobs.ashbyhq.com/gamma/302',
            'https://boards.greenhouse.io/delta/jobs/401',
            'https://jobs.lever.co/epsilon/501',
            'https://jobs.ashbyhq.com/zeta/601',
            'https://boards.greenhouse.io/eta/jobs/701',
        ]
        message=(
            "Please apply to these 10 jobs for me:\n"
            + "\n".join(f"{idx}. [Role {idx}]({url})," if idx % 2 == 0 else f"- {url}" for idx, url in enumerate(links, 1))
            + f"\nDuplicate: {links[0]}"
        )
        with patch('agent_server.threading.Thread') as thread_cls:
            reply=self.client.post('/api/chat',headers=self.headers,json={'message':message})
        self.assertEqual(reply.status_code,200)
        body=reply.json()
        self.assertEqual(body['action'],'apply')
        self.assertIsNotNone(body['run'])
        self.assertEqual(len(body['jobs']),10)
        self.assertTrue(all(item['ready'] for item in body['jobs']))
        self.assertIn('Retrieved 10 link(s) and started applying to 10 job(s)',body['reply'])
        thread_cls.assert_called_once()
        cmd=thread_cls.call_args.kwargs['args'][1]
        self.assertIn('apply',cmd)
        self.assertIn('--jobs',cmd)
        job_ids_arg=cmd[cmd.index('--jobs')+1]
        self.assertEqual(len(job_ids_arg.split(',')),10)
        queued=self.server.query("SELECT job_id, mode, status FROM application_queue ORDER BY job_id")
        self.assertEqual(len(queued),10)
        self.assertTrue(all(row['mode']=='USER_LINK' and row['status']=='Queued' for row in queued))

    def test_chat_mixed_links_applies_ready_and_reports_invalid_or_unsupported(self):
        settings={'unattended':True,'autoSubmit':True,'allowedAutoSubmitAts':['greenhouse'],'autoApplyMinimumScore':60}
        (self.config/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
        jobpilot._JSON_CACHE.clear()
        message=(
            "https://boards.greenhouse.io/acme/jobs/101 "
            "http://insecure.example.com/job/1 "
            "https://example.com/careers/unknown-ats"
        )
        with patch('agent_server.threading.Thread') as thread_cls:
            reply=self.client.post('/api/chat',headers=self.headers,json={'message':message})
        self.assertEqual(reply.status_code,200)
        body=reply.json()
        self.assertEqual(body['action'],'apply')
        self.assertEqual(len(body['jobs']),3)
        self.assertIn('started applying to 1 job(s)',body['reply'])
        self.assertIn('Invalid',body['reply'])
        self.assertIn('ManualReview',body['reply'])
        thread_cls.assert_called_once()

    def test_chat_links_queued_when_worker_is_busy(self):
        settings={'unattended':True,'autoSubmit':True,'allowedAutoSubmitAts':['greenhouse'],'autoApplyMinimumScore':60}
        (self.config/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
        jobpilot._JSON_CACHE.clear()
        self.server._active='busy-run'
        message="https://boards.greenhouse.io/acme/jobs/101\nhttps://boards.greenhouse.io/acme/jobs/102"
        reply=self.client.post('/api/chat',headers=self.headers,json={'message':message})
        self.assertEqual(reply.status_code,200)
        body=reply.json()
        self.assertEqual(body['action'],'custom-link')
        self.assertIsNone(body['run'])
        self.assertIn('queued 2 job(s)',body['reply'])
        queued=self.server.query("SELECT job_id FROM application_queue WHERE status='Queued'")
        self.assertEqual(len(queued),2)
