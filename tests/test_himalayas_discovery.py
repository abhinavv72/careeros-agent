import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import himalayas_discovery
import jobpilot


class HimalayasDiscoveryTests(unittest.TestCase):
    def test_imports_india_and_worldwide_with_platform_handoff(self):
        payload = {'jobs':[
            {'title':'Backend Engineer','companyName':'Example','guid':'example/backend',
             'locationRestrictions':['India'],'applicationLink':'https://himalayas.app/companies/example/jobs/backend?utm_source=api',
             'description':'<p>Build APIs. 2-4 years.</p>','pubDate':1792578348},
            {'title':'Backend Engineer','companyName':'US Only','guid':'us/backend',
             'locationRestrictions':['United States'],'applicationLink':'https://himalayas.app/companies/us/jobs/backend',
             'description':'US only','pubDate':1792578348}]}
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT / 'tmp') as directory:
            root=Path(directory)
            with patch('himalayas_discovery.jobpilot.fetch_json',return_value=payload), \
                 patch('jobpilot.DATA',root), patch('jobpilot.DB',root/'test.db'):
                result=himalayas_discovery.discover('backend')
                self.assertEqual(result['imported'],1)
                db=jobpilot.connect()
                try:
                    row=db.execute('SELECT * FROM jobs').fetchone()
                    self.assertEqual(row['ats'],'himalayas')
                    self.assertEqual(row['source'],'Himalayas')
                    self.assertEqual(row['posted_at'],'')
                    self.assertEqual(row['location'],'Remote India')
                    detail=db.execute('SELECT * FROM job_details WHERE job_id=?',(row['id'],)).fetchone()
                    self.assertEqual(detail['apply_type'],'platform_handoff')
                finally:db.close()


if __name__=='__main__':unittest.main()
