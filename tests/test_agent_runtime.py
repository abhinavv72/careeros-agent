import argparse
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import jobpilot
import ats_forms
from agent_runtime import exclusive_lock, recover_attempts, WorkerBusy


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp')
        self.root = Path(self.tmp.name)
        self.patches = [patch('jobpilot.DATA', self.root), patch('jobpilot.DB', self.root/'test.db')]
        for item in self.patches:
            item.start()
        self.db = jobpilot.connect()
        self.db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats) VALUES(1,'Test','Engineer','today','unique','Queued','lever')")
        self.db.execute("INSERT INTO application_queue(job_id,created_at) VALUES(1,'today')")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def attempt(self, step='open', completed=None):
        self.db.execute("INSERT INTO application_attempts(job_id,started_at,status,step,completed_at) VALUES(1,'today','Running',?,?)", (step, completed))
        self.db.commit()

    def apply_args(self, **kwargs):
        return argparse.Namespace(job=1, confirm_fill=True, unattended=True, dry_run=False, **kwargs)

    def test_lock_excludes_another_process_and_releases_after_exit(self):
        path = self.root/'resource.lock'
        command = [sys.executable, '-c',
                   'import sys; from pathlib import Path; from agent_runtime import exclusive_lock; '
                   'lock=exclusive_lock(Path(sys.argv[1])); lock.__enter__()', str(path)]
        with exclusive_lock(path):
            result = subprocess.run(command, cwd=jobpilot.ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('WorkerBusy', result.stderr)
        self.assertEqual(subprocess.run(command, cwd=jobpilot.ROOT, capture_output=True).returncode, 0)
        with exclusive_lock(path):
            pass

    def test_interrupted_before_submission_can_be_retried(self):
        self.attempt()
        self.assertEqual(recover_attempts(self.db, jobpilot.now()), 1)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'FailedTemporary')
        self.assertEqual(recover_attempts(self.db, jobpilot.now()), 0)

    def test_interrupted_submission_requires_review(self):
        self.attempt('submit')
        recover_attempts(self.db, jobpilot.now())
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'ManualReview')
        self.assertEqual(self.db.execute('SELECT status FROM application_queue').fetchone()[0], 'ReviewRequired')
        with patch('jobpilot._apply_job') as browser:
            with self.assertRaises(SystemExit):
                jobpilot.cmd_apply(self.apply_args())
            browser.assert_not_called()

    def test_confirmation_survives_recovery(self):
        self.attempt('submit')
        self.db.execute("UPDATE jobs SET status='Applied'")
        self.db.commit()
        recover_attempts(self.db, jobpilot.now())
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'Applied')
        self.assertEqual(self.db.execute('SELECT status FROM application_attempts').fetchone()[0], 'Applied')

    def test_retry_all_cannot_requeue_unconfirmed_submission(self):
        self.attempt('submit', 'today')
        self.db.execute("UPDATE jobs SET status='ManualReview',eligibility_reason='Submission confirmation not detected'")
        self.db.commit()
        with patch('jobpilot.eligible', return_value=(True, 'Eligible')):
            jobpilot.cmd_retry(argparse.Namespace(all=True, captcha=True))
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'ManualReview')

    def test_old_checkpoint_blocks_submission_even_if_job_was_requeued(self):
        self.attempt('submit', 'today')
        with patch('jobpilot._apply_job') as browser:
            with self.assertRaises(SystemExit):
                jobpilot.cmd_apply(self.apply_args())
            browser.assert_not_called()

    def test_disabled_submission_settings_block_dashboard_worker(self):
        with patch('jobpilot.load_json', return_value={'unattended':False,'autoSubmit':False}), patch('jobpilot._apply_job') as browser:
            with self.assertRaises(SystemExit):
                jobpilot.cmd_apply(self.apply_args())
            browser.assert_not_called()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM application_attempts').fetchone()[0], 0)

    def test_failed_batch_preparation_releases_lock(self):
        with patch('jobpilot.load_json', return_value={}):
            with self.assertRaises(SystemExit):
                jobpilot.cmd_apply_batch(argparse.Namespace(dry_run=False, limit=1))
        with exclusive_lock(self.root/'application-batch.lock'):
            pass

    def test_cli_batch_shares_dashboard_worker_lock(self):
        with patch('jobpilot._run_application_batch') as run_batch:
            with exclusive_lock(self.root/'agent-worker.lock'):
                with self.assertRaises(WorkerBusy):
                    jobpilot.cmd_apply_batch(argparse.Namespace(dry_run=False, limit=1))
            run_batch.assert_not_called()
        with exclusive_lock(self.root/'application-batch.lock'):
            pass

    def test_batch_reports_failures_after_processing_all_jobs(self):
        self.db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats) VALUES(2,'Test','Engineer','today','second','Queued','lever')")
        self.db.execute("INSERT INTO application_queue(job_id,created_at) VALUES(2,'today')")
        self.db.commit()
        settings={'unattended':True,'autoSubmit':True,'allowedAutoSubmitAts':['lever']}
        with patch('jobpilot.load_json',return_value=settings), patch('jobpilot.cmd_apply',side_effect=[RuntimeError('Fixture failure'),None]) as apply:
            with self.assertRaises(SystemExit):
                jobpilot.cmd_apply_batch(argparse.Namespace(dry_run=False,limit=2))
            self.assertEqual(apply.call_count,2)

    def test_pipeline_exports_results_even_when_batch_fails(self):
        with patch('jobpilot.cmd_discover'), patch('jobpilot.cmd_score'), patch('jobpilot.cmd_queue'), patch('jobpilot.cmd_status'), patch('jobpilot.cmd_export') as export, patch('jobpilot.cmd_apply_batch',side_effect=SystemExit('Fixture failure')):
            with self.assertRaises(SystemExit):
                jobpilot.cmd_run(argparse.Namespace(limit=2,apply=True))
            self.assertEqual(export.call_count,2)

    def test_dashboard_discovery_does_not_score_or_queue(self):
        worker = importlib.import_module('agent_worker')
        args = argparse.Namespace(action='discover', limit=5, dry_run=False)
        with patch('jobpilot.cmd_discover') as discover, patch('jobpilot.cmd_score') as score, patch('jobpilot.cmd_queue') as queue:
            worker.run(args)
        discover.assert_called_once()
        score.assert_not_called()
        queue.assert_not_called()

    def test_only_validated_adapters_are_authorized_for_autosubmit(self):
        self.assertTrue(ats_forms.supports_autosubmit('greenhouse'))
        self.assertTrue(ats_forms.supports_autosubmit('lever'))
        for ats in ('indeed','linkedin','ashby','bamboohr','workable','generic','unknown'):
            with self.subTest(ats=ats):
                self.assertFalse(ats_forms.supports_autosubmit(ats))

    def test_scoring_preserves_queued_application_state(self):
        self.db.execute("INSERT INTO jobs(id,company,title,description,discovered_at,fingerprint,status,ats,score) VALUES(2,'Test','Engineer','.NET','today','another','Queued','lever',75)")
        self.db.execute("INSERT INTO application_queue(job_id,created_at,status) VALUES(2,'today','Queued')")
        self.db.commit()
        config={'profile.json':{'preferences':{'roles':['Engineer'],'locations':['India']}},
                'eligibility.json':{},'skills.json':{}}
        with patch('jobpilot.load_json',side_effect=lambda name:config[name]), patch('jobpilot.eligible',return_value=(False,'Fixture mismatch')):
            jobpilot.cmd_score(argparse.Namespace())
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=2').fetchone()[0],'Queued')
        self.assertEqual(self.db.execute('SELECT status FROM application_queue WHERE job_id=2').fetchone()[0],'Queued')

    def test_queue_step_does_not_requeue_a_submit_checkpoint(self):
        self.db.execute("UPDATE jobs SET status='Qualified' WHERE id=1")
        self.db.execute("DELETE FROM application_queue WHERE job_id=1")
        self.db.execute("INSERT INTO application_attempts(job_id,started_at,completed_at,status,step) VALUES(1,'today','today','ManualReview','submit')")
        self.db.commit()
        with patch('jobpilot.load_json',return_value={'dailyTarget':5}):
            jobpilot.cmd_queue(argparse.Namespace(limit=5))
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=1').fetchone()[0],'Qualified')
        self.assertIsNone(self.db.execute('SELECT status FROM application_queue WHERE job_id=1').fetchone())

    def test_queue_prioritizes_jobs_that_can_reach_one_click_apply(self):
        (self.root/'resumes').mkdir();(self.root/'resumes'/'generic.pdf').write_bytes(b'%PDF-1.4\n%%EOF')
        self.db.execute("UPDATE jobs SET status='Qualified',title='Backend Developer',description='.NET C#',location='Bengaluru India',apply_url='https://jobs.lever.co/test/1',score=70,resume='generic.pdf' WHERE id=1")
        for job_id,ats,location,score in ((2,'indeed','Bengaluru India',99),(3,'lever','Remote - US',90)):
            self.db.execute("INSERT INTO jobs(id,company,title,description,location,discovered_at,fingerprint,status,ats,apply_url,score,resume) VALUES(?,'Test','Backend Developer','.NET C#',?,'today',?,'Qualified',?,?,?,'generic.pdf')",
                            (job_id,location,f'fixture-{job_id}',ats,f'https://jobs.example/{job_id}',score))
        self.db.commit()
        settings={'dailyTarget':1,'unattended':True,'autoSubmit':True,'allowedAutoSubmitAts':['lever']}
        with patch('jobpilot.load_json',side_effect=lambda name:settings if name=='settings.json' else {}):
            jobpilot.cmd_queue(argparse.Namespace(limit=1))
        self.assertEqual(self.db.execute("SELECT id FROM jobs WHERE status='Queued'").fetchall()[0]['id'],1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE status='Qualified'").fetchone()[0],2)

    def test_worker_continues_after_failure_but_reports_failed_run(self):
        worker = importlib.import_module('agent_worker')
        self.db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats) VALUES(2,'Test','Engineer','today','second','Queued','lever')")
        self.db.commit()
        args = argparse.Namespace(action='apply',job=None,limit=2,dry_run=True)
        with patch('agent_worker.stopped',return_value=False), patch('jobpilot.cmd_apply',side_effect=[RuntimeError('Fixture failure'),None]) as apply, patch('jobpilot.cmd_export') as export, patch.dict(os.environ):
            with self.assertRaises(SystemExit):
                worker.run(args)
            self.assertEqual(apply.call_count, 2)
            export.assert_called_once()

    def test_worker_uses_exact_reviewed_batch_ids(self):
        worker=importlib.import_module('agent_worker')
        for job_id in (2,3):
            self.db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats) VALUES(?,'Test','Engineer','today',?,'Queued','lever')",(job_id,f'fixture-{job_id}'))
            self.db.execute("INSERT INTO application_queue(job_id,created_at) VALUES(?,'today')",(job_id,))
        self.db.commit()
        args=argparse.Namespace(action='apply',job=None,jobs='3,1',limit=2,dry_run=False)
        settings={'allowedAutoSubmitAts':['lever']}
        with patch('agent_worker.stopped',return_value=False),patch('jobpilot.load_json',return_value=settings),patch('jobpilot.eligible',return_value=(True,'Fixture')),patch('jobpilot.cmd_apply') as apply,patch('jobpilot.cmd_export'):
            worker.run(args)
        self.assertEqual([call.args[0].job for call in apply.call_args_list],[3,1])

    def test_dashboard_retry_only_promotes_due_network_failures(self):
        worker=importlib.import_module('agent_worker')
        for job_id,reason in ((2,'TimeoutError: network timeout'),(3,'CAPTCHA detected'),(4,'Missing required fields: salary')):
            self.db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats,eligibility_reason) VALUES(?,'Test','Engineer','today',?,'SkippedBlocked','lever',?)",(job_id,f'fixture-{job_id}',reason))
            self.db.execute("INSERT INTO application_queue(job_id,status,created_at) VALUES(?,'SkippedBlocked','today')",(job_id,))
        self.db.commit()
        with patch('jobpilot.eligible',return_value=(True,'Fixture')):
            worker.run(argparse.Namespace(action='retry'))
        statuses={row['id']:row['status'] for row in self.db.execute('SELECT id,status FROM jobs WHERE id IN (2,3,4)')}
        self.assertEqual(statuses,{2:'Queued',3:'SkippedBlocked',4:'SkippedBlocked'})

