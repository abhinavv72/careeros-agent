import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import jobpilot


class NaukriDirectFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=jobpilot.ROOT / 'tmp')
        self.root = Path(self.temp.name)
        self.data_patch = patch('jobpilot.DATA', self.root)
        self.db_patch = patch('jobpilot.DB', self.root / 'test.db')
        self.data_patch.start(); self.db_patch.start()
        self.db = jobpilot.connect()
        self.db.execute("INSERT INTO jobs(id,company,title,description,location,source,source_url,apply_url,ats,discovered_at,fingerprint,status) VALUES(1,'Example','.NET Developer','2-4 years of experience','Hyderabad','Naukri','https://www.naukri.com/job-listings-test','https://www.naukri.com/job-listings-test','naukri',?,'fixture','Queued')",(jobpilot.now(),))
        self.db.execute("INSERT INTO application_queue(job_id,created_at,status) VALUES(1,?,'Queued')",(jobpilot.now(),))
        self.attempt = self.db.execute("INSERT INTO application_attempts(job_id,started_at,status,adapter,step) VALUES(1,?,'Running','naukri','open')",(jobpilot.now(),)).lastrowid
        self.db.commit()
        self.evidence = self.root / 'screenshots' / 'job-1' / f'attempt-{self.attempt}'
        self.evidence.mkdir(parents=True)
        (self.evidence / 'attempt.json').write_text(json.dumps({'resume':'backend.pdf','mode':'AUTO'}),encoding='utf-8')
        self.page = Mock()
        self.page.url = 'https://www.naukri.com/job-listings-test'
        self.page.body_text = 'Apply'
        self.page.locator.return_value.last.count.return_value = 0
        self.page.locator.return_value.first.count.return_value = 0

    def tearDown(self):
        self.db.close()
        self.db_patch.stop(); self.data_patch.stop()
        self.temp.cleanup()

    def run_flow(self, body, apply_result=None, has_apply=True):
        self.page.body_text = body
        control = Mock()
        if apply_result is not None:
            control.click.side_effect = lambda: setattr(self.page, 'body_text', apply_result)
        row = self.db.execute('SELECT * FROM jobs WHERE id=1').fetchone()
        with patch('captcha_solver.send_desktop_notification'), \
             patch('jobpilot.visible_page_text',side_effect=lambda page:page.body_text), \
             patch('jobpilot.naukri_direct_apply_control',return_value=control if has_apply else None), \
             patch('jobpilot.safe_screenshot'), patch('jobpilot.blocking_challenge',return_value=False):
            jobpilot.try_naukri_direct_apply(self.page,self.db,row,self.evidence/'filled.png')
        return self.db.execute('SELECT status FROM jobs WHERE id=1').fetchone()[0]

    def test_naukri_page_metadata_exposes_over_limit_experience_before_apply(self):
        self.page.title.return_value = 'Java API Engineer - 5 to 10 years of experience'
        self.page.locator.return_value.get_attribute.return_value = 'Job description for 5 to 10 years of experience'
        metadata = jobpilot.naukri_page_metadata(self.page)
        row = dict(self.db.execute('SELECT * FROM jobs WHERE id=1').fetchone())
        row['description'] = metadata
        eligible, reason = jobpilot.eligible(row, {'maxExperienceYears': 4, 'indiaOnlyUnlessRemote': True})
        self.assertFalse(eligible)
        self.assertEqual(reason, 'Requires at least 5 years; candidate profile has 3')

    def test_fresh_confirmation_creates_receipt(self):
        status=self.run_flow('Apply', 'Application sent')
        self.assertEqual(status,'Applied')
        self.assertTrue((self.evidence/'receipt.json').is_file())
        self.assertTrue((self.evidence/'checkpoint.json').is_file())

    def test_unconfirmed_click_requires_review_without_retry(self):
        status=self.run_flow('Apply', 'Processing')
        self.assertEqual(status,'ManualReview')
        self.assertFalse((self.evidence/'receipt.json').exists())
        self.assertEqual(self.db.execute('SELECT status FROM application_queue WHERE job_id=1').fetchone()[0],'ReviewRequired')

    def test_login_expiry_never_clicks_apply(self):
        status=self.run_flow('Login to apply', has_apply=False)
        self.assertEqual(status,'SkippedBlocked')
        self.assertFalse((self.evidence/'checkpoint.json').exists())

    def test_external_apply_link_is_handoff(self):
        status=self.run_flow('Apply on company site', has_apply=False)
        self.assertEqual(status,'SkippedBlocked')
        self.assertFalse((self.evidence/'checkpoint.json').exists())


if __name__=='__main__':unittest.main()
