import unittest
from unittest.mock import patch, Mock
import argparse
import sys
import tempfile
from pathlib import Path
import jobpilot
import board_discovery


class BoardDiscoveryTests(unittest.TestCase):
    def test_linkedin_discovery_is_rejected_before_scraper_load(self):
        with patch.dict(sys.modules, {"jobspy":None}):
            with self.assertRaisesRegex(ValueError, "disabled for: linkedin"):
                board_discovery.discover(argparse.Namespace(sites=['linkedin'],keywords='engineer',limit=3))

    def test_missing_values_are_not_strings(self):
        self.assertEqual(board_discovery.text(float('nan')), '')
        self.assertEqual(board_discovery.text(None), '')

    def test_indeed_and_naukri_discovery_maps_to_shared_tracker(self):
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT / 'tmp') as directory:
            root = Path(directory)
            frame = Mock()
            frame.to_dict.return_value = [{"id":"abc", "title":".NET Developer", "company":"Fixture", "description":"C# APIs",
                                          "location":"India", "is_remote":True, "job_url":"https://in.indeed.com/viewjob?jk=abc", "date_posted":"2026-10-12"}]
            module = Mock()
            module.scrape_jobs.return_value = frame
            with patch.dict(sys.modules, {"jobspy":module}), patch('jobpilot.DATA',root), patch('jobpilot.DB',root/'test.db'), patch('jobpilot.cmd_score'), patch('jobpilot.cmd_queue'), patch('jobpilot.cmd_export'):
                board_discovery.discover(argparse.Namespace(sites=['indeed'],keywords='.NET C#',limit=3))
                db=jobpilot.connect()
                row=db.execute('SELECT * FROM jobs').fetchone()
                self.assertEqual(row['ats'],'indeed')
                self.assertEqual(row['location'],'Remote India')
                self.assertFalse(jobpilot.eligible(row,{"requireDotnet":True,"remoteOnly":True,"maxPostingHours":24})[0])
                db.close()
