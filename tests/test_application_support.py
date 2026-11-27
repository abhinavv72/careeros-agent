import argparse
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import jobpilot
import profile_agent
from application_support import atomic_json, candidate_values, recovery_guidance, retry_time, snapshot_resume, unavailable_reason


class ApplicationSupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp')
        self.root=Path(self.tmp.name)
        (self.root/'resumes').mkdir()
        (self.root/'resumes'/'generic.pdf').write_bytes(b'%PDF-1.4\nfirst resume\n%%EOF')
        self.patches=[patch('jobpilot.DATA',self.root),patch('jobpilot.DB',self.root/'test.db')]
        for item in self.patches:item.start()
        self.db=jobpilot.connect()
        self.db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats,resume) VALUES(1,'Fixture','Engineer','today','unique','Queued','lever','generic.pdf')")
        self.db.execute("INSERT INTO application_queue(job_id,created_at) VALUES(1,'today')")
        self.db.commit()
        self.settings={'unattended':True,'autoSubmit':True,'allowedAutoSubmitAts':['lever'],
                       'retry':{'maxAttempts':3,'baseDelaySeconds':60,'maxDelaySeconds':90}}

    def tearDown(self):
        self.db.close()
        for item in reversed(self.patches):item.stop()
        self.tmp.cleanup()

    def args(self,dry=False):
        return argparse.Namespace(job=1,confirm_fill=True,unattended=True,dry_run=dry)

    def test_atomic_json_retries_temporary_windows_replace_lock(self):
        path=self.root/'profile.json';path.write_text('{"old":true}',encoding='utf-8')
        original=type(path).replace
        calls=[]
        def busy_once(source,target):
            calls.append(1)
            if len(calls)==1:raise PermissionError('File briefly locked')
            return original(source,target)
        with patch.object(type(path),'replace',busy_once),patch('application_support.time.sleep') as sleep:
            atomic_json(path,{'new':True})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),{'new':True})
        sleep.assert_called_once()

    def test_candidate_values_contain_only_supplied_facts(self):
        values=candidate_values({'personal':{'firstName':'Test','lastName':'Candidate'},'experience':{}},{})
        self.assertEqual(values['full name'],'Test Candidate')
        for key in ('city','current company','notice period','current compensation','what tech stack','incredibly fast paced'):
            self.assertNotIn(key,values)

    def test_personal_bank_default_is_never_used_without_saved_fact(self):
        config={'answers.json':{},'profile.json':{},'question-bank.json':[
            {'key':'fast_paced','patterns':['comfortable in a fast paced environment'],'autoFill':True,'answer':'Yes'}]}
        with patch('jobpilot.load_json',side_effect=config.__getitem__):
            self.assertIsNone(jobpilot.question_answer('comfortable in a fast paced environment')[0])
            config['answers.json']['fast_paced']='No'
            self.assertEqual(jobpilot.question_answer('comfortable in a fast paced environment')[0],'No')

    def test_tool_experience_requires_a_tool_specific_fact(self):
        original=jobpilot.load_json
        with patch('jobpilot.load_json',side_effect=lambda name:{'tool_experience_years':{'Temporal':1}} if name=='answers.json' else original(name)):
            self.assertEqual(jobpilot.question_answer('Years of experience in Temporal')[0],'1')
            self.assertIsNone(jobpilot.question_answer('Years of experience in Flink')[0])

    def test_profile_does_not_assume_cloud_or_notice_period(self):
        self.assertIsNone(profile_agent.answer('Have you worked with any cloud platforms?',{},{}))
        self.assertIsNone(profile_agent.answer('Desired start date',{},{}))

    def test_combined_compensation_does_not_raise_or_invent_values(self):
        original=jobpilot.load_json
        with patch('jobpilot.load_json',side_effect=lambda name:{} if name=='answers.json' else original(name)):
            self.assertIsNone(jobpilot.question_answer('Current and expected compensation')[0])

    def test_resume_snapshot_survives_source_changes(self):
        folder=self.root/'attempt';folder.mkdir()
        source=self.root/'resumes'/'generic.pdf'
        expected=source.read_bytes()
        target,metadata=snapshot_resume(source,folder)
        source.write_bytes(b'%PDF-1.4\nreplacement')
        self.assertEqual(target.read_bytes(),expected)
        self.assertEqual(metadata['sha256'],hashlib.sha256(expected).hexdigest())

    def test_invalid_resume_is_rejected_before_browser_opens(self):
        (self.root/'resumes'/'generic.pdf').write_text('not a PDF')
        with patch('jobpilot.load_json',return_value=self.settings),patch('jobpilot._apply_job') as browser:
            with self.assertRaises(ValueError):jobpilot.cmd_apply(self.args())
            browser.assert_not_called()
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0],'SkippedBlocked')

    def test_backoff_is_capped_and_stops_at_attempt_limit(self):
        time='2026-10-13T10:00:00+00:00'
        self.assertEqual(retry_time(self.settings,1,time),'2026-10-13T10:01:00+00:00')
        self.assertEqual(retry_time(self.settings,2,time),'2026-10-13T10:01:30+00:00')
        self.assertIsNone(retry_time(self.settings,3,time))

    def test_only_network_errors_get_automatic_retry(self):
        for reason,category in [('TimeoutError: network timeout','network'),('Missing required fields: sponsorship','missing_answers'),
                                ('Naukri login session expired','login'),('CAPTCHA detected','human_challenge'),
                                ('Selected resume is invalid','resume'),('Job closed or removed: HTTP 410','expired')]:
            guidance=recovery_guidance('SkippedBlocked',reason)
            self.assertEqual(guidance['category'],category)
            self.assertEqual(guidance['automatic_retry'],category=='network')
        self.assertFalse(recovery_guidance('FailedTemporary','network timeout',True)['automatic_retry'])
        self.assertEqual(recovery_guidance('ManualReview','CAPTCHA detected')['category'],'human_challenge')

    def test_recovered_network_attempt_gets_a_delayed_retry(self):
        self.db.execute("INSERT INTO application_attempts(job_id,started_at,status,step) VALUES(1,'today','Running','open')")
        self.db.execute('UPDATE application_queue SET attempts=1');self.db.commit()
        with patch('jobpilot.load_json',return_value=self.settings):jobpilot.recover_application_attempts(self.db)
        self.assertTrue(self.db.execute('SELECT next_attempt_at FROM application_queue').fetchone()[0])
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0],'FailedTemporary')

    def test_network_failure_schedules_retry_and_cannot_run_early(self):
        with patch('jobpilot.load_json',return_value=self.settings),patch('jobpilot._apply_job',side_effect=TimeoutError('network timeout')):
            with self.assertRaises(TimeoutError):jobpilot.cmd_apply(self.args())
        queue=self.db.execute('SELECT * FROM application_queue').fetchone()
        self.assertTrue(queue['next_attempt_at'])
        self.assertEqual(queue['attempts'],1)
        with patch('jobpilot.load_json',return_value=self.settings),patch('jobpilot._apply_job') as browser:
            with self.assertRaises(SystemExit):jobpilot.cmd_apply(self.args())
            browser.assert_not_called()

    def test_due_retry_is_promoted_but_submission_checkpoint_never_is(self):
        self.db.execute("UPDATE jobs SET status='FailedTemporary'")
        self.db.execute("UPDATE application_queue SET status='FailedTemporary',attempts=1,next_attempt_at='2020-01-01T00:00:00+00:00'")
        self.db.commit()
        with patch('jobpilot.load_json',return_value=self.settings):jobpilot.promote_due_retries(self.db)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0],'Queued')
        self.db.execute("UPDATE jobs SET status='FailedTemporary'")
        self.db.execute("UPDATE application_queue SET next_attempt_at='2020-01-01T00:00:00+00:00'")
        self.db.execute("INSERT INTO application_attempts(job_id,started_at,completed_at,step) VALUES(1,'today','today','submit')")
        self.db.commit()
        with patch('jobpilot.load_json',return_value=self.settings):jobpilot.promote_due_retries(self.db)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0],'FailedTemporary')

    def test_attempt_limit_blocks_browser(self):
        self.db.execute('UPDATE application_queue SET attempts=3');self.db.commit()
        with patch('jobpilot.load_json',return_value=self.settings),patch('jobpilot._apply_job') as browser:
            with self.assertRaises(SystemExit):jobpilot.cmd_apply(self.args())
            browser.assert_not_called()

    def test_dry_run_preserves_retry_schedule_and_prior_evidence(self):
        self.db.execute("UPDATE jobs SET status='FailedTemporary',eligibility_reason='network timeout'")
        self.db.execute("UPDATE application_queue SET status='FailedTemporary',attempts=1,next_attempt_at='2099-01-01T00:00:00+00:00'")
        self.db.commit()
        def inspect(args):
            (args.evidence_dir/'filled.png').write_bytes(str(args.attempt_id).encode())
        with patch('jobpilot.load_json',return_value=self.settings),patch('jobpilot._apply_job',side_effect=inspect):
            jobpilot.cmd_apply(self.args(True))
            jobpilot.cmd_apply(self.args(True))
        rows=self.db.execute('SELECT id,screenshot_path FROM application_attempts ORDER BY id').fetchall()
        self.assertEqual(len(rows),2)
        for row in rows:self.assertEqual(Path(row['screenshot_path']).read_bytes(),str(row['id']).encode())
        self.assertEqual(self.db.execute('SELECT attempts,next_attempt_at FROM application_queue').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0],'FailedTemporary')
        self.assertTrue(self.db.execute('SELECT next_attempt_at FROM application_queue').fetchone()[0].startswith('2099'))

    def test_receipt_and_export_record_real_submission_and_resume(self):
        def submitted(args):
            db=jobpilot.connect()
            try:
                jobpilot.mark_submission_started(db,1)
                job=self.db.execute('SELECT * FROM jobs WHERE id=1').fetchone()
                jobpilot.record_submission_receipt(db,job,Mock(url='https://employer.test/confirmation'),'Thank you for applying')
            finally:db.close()
        with patch('jobpilot.load_json',return_value=self.settings),patch('jobpilot._apply_job',side_effect=submitted):
            jobpilot.cmd_apply(self.args())
        receipt=json.loads((self.root/'screenshots'/'job-1'/'attempt-1'/'receipt.json').read_text())
        self.assertEqual(receipt['confirmation_text'],'Thank you for applying')
        self.assertEqual(receipt['resume']['filename'],'generic.pdf')
        self.assertEqual(receipt['resume']['sha256'],hashlib.sha256((self.root/'resumes'/'generic.pdf').read_bytes()).hexdigest())
        jobpilot.cmd_export(argparse.Namespace())
        with (self.root/'exports'/'JobApplications.csv').open(encoding='utf-8-sig',newline='') as handle:
            row=next(csv.DictReader(handle))
        self.assertTrue(row['DateApplied'])
        self.assertEqual(row['Mode'],'AUTO')
        self.assertTrue((self.root/'screenshots'/'job-1'/'attempt-1'/'checkpoint.json').is_file())

    def test_expiry_detection_does_not_match_job_description_keywords(self):
        self.assertIsNotNone(unavailable_reason('This position has been filled.'))
        self.assertIsNotNone(unavailable_reason('Job not found',404))
        self.assertIsNone(unavailable_reason('Build software that manages expired invoices and closed tickets.'))

