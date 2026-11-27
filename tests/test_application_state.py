"""Phase 0 exit gate: stale queue rows, simultaneous workers and crashes around the
final submit click must never produce a second submission."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import jobpilot
import application_state as st
from agent_runtime import recover_attempts


class ApplicationStateTests(unittest.TestCase):
    def setUp(self):
        (jobpilot.ROOT / 'tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=jobpilot.ROOT / 'tmp')
        self.root = Path(self.tmp.name)
        config = self.root / 'config'; config.mkdir()
        for name in ('profile.json', 'eligibility.json', 'settings.json', 'answers.json', 'skills.json'):
            (config / name).write_text('{}', encoding='utf-8')
        self.patches = [patch('jobpilot.DATA', self.root), patch('jobpilot.DB', self.root / 'test.db'),
                        patch('jobpilot.CONFIG', config),
                        patch.dict(os.environ, {}, clear=False)]
        for item in self.patches: item.start()
        os.environ.pop('JOBPILOT_STOP_FILE', None)
        os.environ.pop('JOBPILOT_AUTONOMOUS', None)
        jobpilot._JSON_CACHE.clear()
        jobpilot.connect().close()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        jobpilot._JSON_CACHE.clear()
        self.tmp.cleanup()

    # ------------------------------------------------------------ helpers
    def add_job(self, job_id, url, company='Acme', status='Queued', queue='Queued'):
        db = jobpilot.connect()
        db.execute("""INSERT INTO jobs(id,company,title,apply_url,source_url,ats,discovered_at,fingerprint,status)
            VALUES(?,?,?,?,?,'greenhouse','today',?,?)""", (job_id, company, 'Backend Engineer', url, url, f'fp-{job_id}', status))
        if queue:
            db.execute("INSERT INTO application_queue(job_id,created_at,status,score) VALUES(?,'today',?,80)", (job_id, queue))
        db.commit(); db.close()

    def open_attempt(self, job_id, step='open'):
        db = jobpilot.connect()
        attempt = db.execute("INSERT INTO application_attempts(job_id,started_at,status,adapter,step) VALUES(?,'today','Running','greenhouse',?)",
                             (job_id, step)).lastrowid
        db.commit(); db.close()
        return attempt

    def state(self, job_id):
        db = jobpilot.connect()
        try: return st.application_state(db, job_id, self.root)
        finally: db.close()

    def attempt(self, job_id, apply_job):
        """Run the real attempt wrapper with the browser part replaced."""
        with patch('jobpilot._apply_job', side_effect=apply_job) as browser:
            try:
                jobpilot._run_application_attempt(argparse.Namespace(job=job_id, confirm_fill=True, unattended=False, dry_run=False))
            except (Exception, SystemExit, KeyboardInterrupt) as exc:
                return browser.call_count, exc
        return browser.call_count, None

    # ------------------------------------------------------------ state derivation
    def test_applied_without_receipt_is_uncertain_and_with_receipt_is_confirmed(self):
        self.add_job(1, 'https://job-boards.greenhouse.io/acme/jobs/111', status='Applied', queue='Applied')
        self.assertEqual(self.state(1).state, st.OUTCOME_UNCERTAIN)
        attempt = self.open_attempt(1, 'submit')
        db = jobpilot.connect(); db.execute("UPDATE application_attempts SET status='Applied',completed_at='today' WHERE id=?", (attempt,)); db.commit(); db.close()
        self.assertEqual(self.state(1).state, st.OUTCOME_UNCERTAIN)
        receipt = self.root / 'screenshots' / 'job-1' / f'attempt-{attempt}' / 'receipt.json'
        receipt.parent.mkdir(parents=True); receipt.write_text('{}', encoding='utf-8')
        self.assertEqual(self.state(1).state, st.CONFIRMED)

    def test_legacy_synonyms_map_to_one_state(self):
        self.add_job(1, 'https://job-boards.greenhouse.io/acme/jobs/1', status='ManualReview', queue='ReviewRequired')
        self.add_job(2, 'https://job-boards.greenhouse.io/acme/jobs/2', status='Queued', queue='Queued')
        self.add_job(3, 'https://job-boards.greenhouse.io/acme/jobs/3', status='Queued', queue='Skipped')
        self.assertEqual(self.state(1).state, st.BLOCKED)
        self.assertEqual(self.state(2).state, st.READY)
        self.assertEqual(self.state(3).state, st.BLOCKED)
        self.assertIn('Stale queue state', self.state(3).reason)

    def test_canonical_url_keeps_job_identity_and_drops_tracking(self):
        self.assertEqual(st.canonical_url('https://www.stripe.com/jobs/search?gh_jid=81&utm_source=x'),
                         st.canonical_url('https://stripe.com/jobs/search/?gh_src=abc&gh_jid=81'))
        self.assertNotEqual(st.canonical_url('https://stripe.com/jobs/search?gh_jid=81'),
                            st.canonical_url('https://stripe.com/jobs/search?gh_jid=82'))
        self.assertEqual(st.canonical_url('https://jobs.lever.co/acme/abc/apply'), st.canonical_url('https://jobs.lever.co/acme/abc'))

    # ------------------------------------------------------------ exit gate
    def test_stale_queue_row_with_checkpoint_is_not_reopened(self):
        self.add_job(1, 'https://job-boards.greenhouse.io/acme/jobs/1')
        attempt = self.open_attempt(1, 'submit')
        db = jobpilot.connect(); db.execute("UPDATE application_attempts SET completed_at='today',status='ManualReview' WHERE id=?", (attempt,)); db.commit(); db.close()
        # Tracker and queue both still say Queued (the stale snapshot found in the live DB).
        self.assertEqual(self.state(1).state, st.OUTCOME_UNCERTAIN)
        calls, error = self.attempt(1, lambda args: None)
        self.assertEqual(calls, 0)
        self.assertIsInstance(error, SystemExit)

    def test_cross_posted_duplicate_is_refused_before_browser_and_at_claim(self):
        self.add_job(1, 'https://boards.greenhouse.io/acme/jobs/4242', status='Applied', queue='Applied')
        self.add_job(2, 'https://acme.com/careers/job?gh_jid=4242&utm_source=linkedin', company='ACME Inc')
        db = jobpilot.connect()
        try:
            with self.assertRaises(st.SubmissionBlocked):
                st.assert_can_start(db, 2, self.root)
        finally: db.close()
        calls, error = self.attempt(2, lambda args: None)
        self.assertEqual(calls, 0)
        # Even with a stale open attempt the final boundary refuses.
        attempt = self.open_attempt(2)
        db = jobpilot.connect()
        try:
            with self.assertRaises(st.SubmissionBlocked):
                st.claim_submission(db, 2, attempt, self.root)
            self.assertEqual(db.execute('SELECT step FROM application_attempts WHERE id=?', (attempt,)).fetchone()[0], 'open')
        finally: db.close()

    def test_simultaneous_workers_claim_one_opening_once(self):
        # Two listings of the same requisition, each prepared by a different worker.
        self.add_job(1, 'https://job-boards.greenhouse.io/acme/jobs/777')
        self.add_job(2, 'https://boards.greenhouse.io/acme/jobs/777?gh_src=feed')
        attempts = {1: self.open_attempt(1), 2: self.open_attempt(2)}
        barrier, results = threading.Barrier(2), {}

        def worker(job_id):
            db = sqlite3.connect(self.root / 'test.db', timeout=30, check_same_thread=False)
            db.row_factory = sqlite3.Row
            try:
                barrier.wait()
                st.claim_submission(db, job_id, attempts[job_id], self.root)
                results[job_id] = 'claimed'
            except st.SubmissionBlocked:
                results[job_id] = 'blocked'
            finally: db.close()

        threads = [threading.Thread(target=worker, args=(job_id,)) for job_id in (1, 2)]
        for t in threads: t.start()
        for t in threads: t.join(30)
        self.assertEqual(sorted(results.values()), ['blocked', 'claimed'])
        db = jobpilot.connect()
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM application_attempts WHERE step='submit'").fetchone()[0], 1)
        finally: db.close()

    def test_second_attempt_on_same_job_cannot_claim(self):
        self.add_job(1, 'https://job-boards.greenhouse.io/acme/jobs/5')
        first, second = self.open_attempt(1), self.open_attempt(1)
        db = jobpilot.connect()
        try:
            # Two in-flight attempts for one job is itself unsafe: neither may claim.
            with self.assertRaises(st.SubmissionBlocked):
                st.claim_submission(db, 1, first, self.root)
            db.execute("UPDATE application_attempts SET completed_at='today' WHERE id=?", (second,)); db.commit()
            st.claim_submission(db, 1, first, self.root)
            second = db.execute("INSERT INTO application_attempts(job_id,started_at,step) VALUES(1,'today','open')").lastrowid
            db.commit()
            with self.assertRaises(st.SubmissionBlocked):
                st.claim_submission(db, 1, second, self.root)
        finally: db.close()

    def test_crash_after_claim_leaves_outcome_uncertain_and_is_never_retried(self):
        self.add_job(1, 'https://job-boards.greenhouse.io/acme/jobs/9')

        def browser_then_crash(args):
            db = jobpilot.connect()
            try: jobpilot.mark_submission_started(db, 1)
            finally: db.close()
            raise TimeoutError('portal hung after submit click')

        calls, error = self.attempt(1, browser_then_crash)
        self.assertEqual(calls, 1)
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(self.state(1).state, st.OUTCOME_UNCERTAIN)
        self.assertTrue(list((self.root / 'screenshots' / 'job-1').glob('attempt-*/checkpoint.json')))
        # Retry sweep and a second worker both leave it alone.
        with patch('jobpilot.eligible', return_value=(True, '')):
            jobpilot.cmd_retry(argparse.Namespace(all=True, captcha=True))
        db = jobpilot.connect()
        try: self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=1').fetchone()[0], 'ManualReview')
        finally: db.close()
        calls, error = self.attempt(1, browser_then_crash)
        self.assertEqual(calls, 0)
        self.assertIsInstance(error, SystemExit)

    def test_hard_process_crash_recovery_distinguishes_before_and_after_submit(self):
        self.add_job(1, 'https://job-boards.greenhouse.io/acme/jobs/10')
        self.add_job(2, 'https://job-boards.greenhouse.io/acme/jobs/20')
        a1, a2 = self.open_attempt(1), self.open_attempt(2)
        db = jobpilot.connect(); st.claim_submission(db, 1, a1, self.root); db.close()
        self.assertEqual(self.state(1).state, st.SUBMITTING)
        self.assertEqual(self.state(2).state, st.PREPARING)
        # Process killed: attempts never completed. The next lock holder recovers them.
        db = jobpilot.connect(); recover_attempts(db, 'later'); db.close()
        self.assertEqual(self.state(1).state, st.OUTCOME_UNCERTAIN)
        self.assertEqual(self.state(2).state, st.BLOCKED)  # FailedTemporary without a retry slot
        db = jobpilot.connect()
        try:
            with self.assertRaises(st.SubmissionBlocked):
                st.assert_can_start(db, 1, self.root)
            st.assert_can_start(db, 2, self.root)  # crashed before submit: safe
        finally: db.close()

    def test_pause_marker_stops_autonomous_submit_at_final_boundary(self):
        self.add_job(1, 'https://job-boards.greenhouse.io/acme/jobs/30')
        attempt = self.open_attempt(1)
        (self.root / st.PAUSE_MARKER).write_text('now', encoding='utf-8')
        db = jobpilot.connect()
        try:
            with self.assertRaises(KeyboardInterrupt):
                st.claim_submission(db, 1, attempt, self.root, autonomous=True)
            self.assertEqual(db.execute('SELECT step FROM application_attempts WHERE id=?', (attempt,)).fetchone()[0], 'open')
            os.environ['JOBPILOT_AUTONOMOUS'] = '1'
            with self.assertRaises(KeyboardInterrupt):
                st.claim_submission(db, 1, attempt, self.root)
            # A user-driven single application is not autonomous work.
            st.claim_submission(db, 1, attempt, self.root, autonomous=False)
            self.assertEqual(db.execute('SELECT step FROM application_attempts WHERE id=?', (attempt,)).fetchone()[0], 'submit')
        finally: db.close()


if __name__ == '__main__':
    unittest.main()
