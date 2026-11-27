import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jobpilot
import remote_discovery


class RemoteDiscoveryTests(unittest.TestCase):
    def test_imports_india_eligible_remote_lead_with_source_attribution(self):
        items = {'jobs': [
            {'id': 1, 'title': 'Backend Engineer', 'company_name': 'Example',
             'candidate_required_location': 'India', 'url': 'https://remotive.com/remote-jobs/software-development/backend-engineer-1',
             'description': '<p>Python APIs, 2-4 years of experience</p>', 'publication_date': '2026-10-21T08:00:00'},
            {'id': 2, 'title': 'Backend Engineer', 'company_name': 'Other',
             'candidate_required_location': 'USA', 'url': 'https://remotive.com/remote-jobs/software-development/backend-engineer-2',
             'description': 'USA only', 'publication_date': '2026-10-21T08:00:00'}]}
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT / 'tmp') as directory:
            root = Path(directory)
            with patch('remote_discovery.jobpilot.fetch_json', return_value=items), \
                 patch('jobpilot.DATA', root), patch('jobpilot.DB', root / 'test.db'):
                outcome = remote_discovery.discover()
                self.assertEqual(outcome['imported'], 1)
                db = jobpilot.connect()
                try:
                    row = db.execute('SELECT * FROM jobs').fetchone()
                    self.assertEqual(row['source'], 'Remotive')
                    self.assertEqual(row['ats'], 'remotive')
                    self.assertEqual(row['location'], 'Remote India')
                    self.assertIn('2-4 years', row['description'])
                    self.assertFalse(db.execute('SELECT * FROM jobs WHERE external_id=?', ('2',)).fetchone())
                finally:
                    db.close()


if __name__ == '__main__':
    unittest.main()
