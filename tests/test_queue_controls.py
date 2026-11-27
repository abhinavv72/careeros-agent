import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import jobpilot
import agent_server


class QueueControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp')
        self.root=Path(self.tmp.name)
        config=self.root/'config';config.mkdir()
        for name,value in [('profile.json',{}),('eligibility.json',{}),('settings.json',{}),('answers.json',{}),('skills.json',{})]:
            (config/name).write_text(json.dumps(value),encoding='utf-8')
        self.patches=[patch('jobpilot.DATA',self.root),patch('jobpilot.DB',self.root/'test.db'),patch('jobpilot.CONFIG',config)]
        for item in self.patches:item.start()
        agent_server._active=None
        agent_server.initialize()

    def tearDown(self):
        agent_server._active=None
        for item in reversed(self.patches):item.stop()
        jobpilot._JSON_CACHE.clear()
        self.tmp.cleanup()

    def test_skip_promotes_next_qualified_job(self):
        db=jobpilot.connect()
        db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status) VALUES(1,'SkipCo','Engineer','today','skip-unique','Queued')")
        db.execute("INSERT INTO application_queue(job_id,created_at,mode,status,score) VALUES(1,'today','AUTO','Queued',80)")
        db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,score,priority) VALUES(2,'NextCo','Backend Engineer','today','next-unique','Qualified',75,1)")
        db.commit();db.close()
        with patch('jobpilot.queue_candidates',return_value=[{'id':2,'company':'NextCo','title':'Backend Engineer','score':75,'priority':1}]):
            result=agent_server.reject_job(1)
        self.assertEqual(result['replacement']['job_id'],2)
        db=jobpilot.connect()
        try:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=1').fetchone()['status'],'Rejected')
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=2').fetchone()['status'],'Queued')
            self.assertEqual([row['job_id'] for row in db.execute('SELECT job_id FROM application_queue')],[2])
        finally:db.close()

    def test_submission_checkpoint_prevents_skip(self):
        db=jobpilot.connect()
        db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status) VALUES(1,'Fixture','Engineer','today','checkpoint-unique','Queued')")
        db.execute("INSERT INTO application_queue(job_id,created_at,mode,status) VALUES(1,'today','AUTO','Queued')")
        db.execute("INSERT INTO application_attempts(job_id,started_at,step) VALUES(1,'today','submit')")
        db.commit();db.close()
        with self.assertRaises(agent_server.HTTPException) as error:
            agent_server.reject_job(1)
        self.assertEqual(error.exception.status_code,409)

    def test_worker_log_marks_current_job_as_locked(self):
        run_dir=self.root/'agent-runs';run_dir.mkdir()
        log=run_dir/'run.log';log.write_text('Opening job 1: Fixture — Engineer\n',encoding='utf-8')
        db=jobpilot.connect()
        db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status) VALUES(1,'Fixture','Engineer','today','active-unique','Queued')")
        db.execute("INSERT INTO application_queue(job_id,created_at,mode,status) VALUES(1,'today','AUTO','Queued')")
        db.execute("INSERT INTO agent_runs(id,action,status,started_at,log_path) VALUES('run-1','apply','Running','today',?)",(str(log),))
        db.commit();db.close()
        agent_server._active='run-1'
        with self.assertRaises(agent_server.HTTPException) as error:
            agent_server.reject_job(1)
        self.assertEqual(error.exception.status_code,409)

    def test_pasted_link_is_queued_once_before_inspection(self):
        url = 'https://www.naukri.com/job-listings-fixture'
        first = jobpilot.import_custom_job(url)
        second = jobpilot.import_custom_job(url + '?utm_source=board')
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], 'Queued')
        db = jobpilot.connect()
        try:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM application_queue').fetchone()[0], 1)
        finally:
            db.close()

    def test_startup_reconciles_stale_queue_snapshots_without_losing_review_items(self):
        db=jobpilot.connect()
        for job_id,status in ((1,'Skipped'),(2,'ManualReview'),(3,'Applied')):
            db.execute('INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status) VALUES(?,?,?,?,?,?)',
                       (job_id,'Fixture',f'Engineer {job_id}','today',f'stale-{job_id}',status))
            db.execute("INSERT INTO application_queue(job_id,created_at,mode,status) VALUES(?,'today','AUTO','Queued')",(job_id,))
        db.commit();db.close()
        agent_server.initialize()
        db=jobpilot.connect()
        try:
            statuses={row['job_id']:row['status'] for row in db.execute('SELECT job_id,status FROM application_queue')}
        finally:db.close()
        self.assertEqual(statuses,{1:'Skipped',2:'ReviewRequired',3:'Applied'})
        self.assertEqual([row['id'] for row in agent_server.jobs_queue()],[2])


if __name__=='__main__':
    unittest.main()
