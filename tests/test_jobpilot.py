import sqlite3
import unittest
import argparse
import json
import tempfile
import io
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import jobpilot
from ats_forms import supports_autosubmit


class JobPilotTests(unittest.TestCase):
    def test_email_code_is_consumed_once_and_not_stored(self):
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp') as directory:
            root=Path(directory)
            flag=root/'run.continue';code_path=root/'run.code'
            code_path.write_text('ABCDEF12')
            page=Mock();fields=[Mock() for _ in range(8)]
            page.locator.return_value.count.return_value=8
            page.locator.return_value.all.return_value=fields
            submit=Mock();submit.is_enabled.return_value=True
            with patch.dict('os.environ',{'JOBPILOT_DASHBOARD':'1','JOBPILOT_CONTINUE_FILE':str(flag),'JOBPILOT_CURRENT_JOB':'1'},clear=True),patch('jobpilot.DATA',root),patch('jobpilot.visible_page_text',return_value='Enter verification code'),patch('gmail_verifier.fetch_latest_verification_code',return_value=None):
                jobpilot.complete_email_verification(page,page,submit)
            self.assertFalse(code_path.exists())
            self.assertEqual(''.join(field.fill.call_args.args[0] for field in fields),'ABCDEF12')
            submit.click.assert_called_once()

    def test_initial_navigation_retries_only_transient_errors(self):
        page=Mock()
        page.goto.side_effect=[RuntimeError('ERR_NAME_NOT_RESOLVED'),None]
        jobpilot.open_application(page,'https://example.com/job')
        self.assertEqual(page.goto.call_count,2)
        page.goto.reset_mock()
        page.goto.side_effect=RuntimeError('ERR_ACCESS_DENIED')
        with self.assertRaises(RuntimeError):jobpilot.open_application(page,'https://example.com/job')
        self.assertEqual(page.goto.call_count,1)
    def test_numeric_experience_range_contains_actual_value(self):
        self.assertEqual(jobpilot.matching_option('3',['0-1 years','1-3 years','3-5 years']),1)
        self.assertIsNone(jobpilot.matching_option('3',['5-7 years','8-10 years']))
        self.assertEqual(jobpilot.matching_option('3',['Less than 6 months','1-2 years','More than 2 years']),2)
        self.assertIsNone(jobpilot.matching_option('2',['More than 2 years']))
    def test_remote_dotnet_recent_filters(self):
        from datetime import datetime, timedelta, timezone
        job = {"title":".NET Developer", "description":"C# ASP.NET Core backend", "location":"Remote India", "posted_at":datetime.now(timezone.utc).isoformat()}
        rules = {"requireDotnet":True,"remoteOnly":True,"maxPostingHours":24}
        self.assertTrue(jobpilot.eligible(job, rules)[0])
        job['posted_at'] = (datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
        self.assertFalse(jobpilot.eligible(job, rules)[0])
        job['posted_at'] = ''
        self.assertFalse(jobpilot.eligible(job, rules)[0])
        job['location'] = 'Hybrid India'
        self.assertFalse(jobpilot.eligible(job, rules)[0])

    def test_remote_country_scope_must_include_india_or_worldwide(self):
        job={'title':'Backend Software Engineer','description':'C# and .NET','location':'Remote','posted_at':''}
        self.assertEqual(jobpilot.eligible(job,{})[1],'Remote country scope not confirmed')
        job['location']='Remote - US'
        self.assertEqual(jobpilot.eligible(job,{})[1],'Remote role is restricted to a region outside India')
        job['location']='Remote Spain'
        self.assertEqual(jobpilot.eligible(job,{})[1],'Remote role is restricted to a region outside India')
        job['location']='Remote - India'
        self.assertTrue(jobpilot.eligible(job,{})[0])
        job['location']='Worldwide Remote'
        self.assertTrue(jobpilot.eligible(job,{})[0])

    def test_sales_titles_are_not_qualified_as_software_roles(self):
        job={'title':'Software Sales Engineer','description':'C# .NET platform','location':'India'}
        self.assertEqual(jobpilot.eligible(job,{})[1],'Role family mismatch')

    def test_lever_includes_requirements(self):
        with patch("jobpilot.fetch_json", return_value=[{"id":"1", "text":"Engineer", "hostedUrl":"https://jobs.lever.co/test/1", "descriptionPlain":"Intro", "lists":[{"text":"Requirements", "content":"<li>8 years Python experience</li>"}], "additionalPlain":"Closing"}]):
            description = list(jobpilot.lever({"boardId":"test", "company":"Test"}))[0].description
            self.assertIn("8 years Python experience", description)
            self.assertIn("Closing", description)

    def test_workable_public_careers_feed_normalizes_postings(self):
        payload={"jobs":[
            {"id":"internal-1","shortcode":"ABC123","title":"Backend Engineer","state":"published",
             "description":"<p>C# and .NET APIs</p>","url":"https://apply.workable.com/acme/j/ABC123/",
             "application_url":"https://apply.workable.com/acme/j/ABC123/","created_at":"2026-10-20T00:00:00Z",
             "location":{"location_str":"Hyderabad, India"}},
            {"id":"draft-1","title":"Draft role","state":"draft"}
        ]}
        with patch("jobpilot.fetch_json",return_value=payload):
            jobs=list(jobpilot.workable({"boardId":"acme","company":"Acme"}))
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0].title,"Backend Engineer")
        self.assertEqual(jobs[0].ats,"workable")
        self.assertIn("C# and .NET APIs",jobs[0].description)
        self.assertEqual(jobs[0].location,"Hyderabad, India")

    def test_workday_search_uses_configured_term_and_saved_target_roles(self):
        company = {"searchText": "India"}
        profile = {"preferences": {"roles": ["Backend Developer", "Software Engineer", "Full Stack Developer", "Platform Engineer"]}}
        self.assertEqual(jobpilot.workday_search_terms(company, profile), [
            "India", "Backend Developer India", "Software Engineer India", "Full Stack Developer India"
        ])

    def test_workday_paginates_and_deduplicates_across_role_queries(self):
        company = {"company": "Example", "tenant": "example", "board": "External", "domain": "example.wd5.myworkdayjobs.com",
                   "searchText": "Backend Developer India"}
        responses = [
            {"total": 3, "jobPostings": [
                {"externalPath": "/job/one", "title": "Backend Engineer", "locationsText": "India", "postedOn": "Today"},
                {"externalPath": "/job/two", "title": "Software Engineer", "locationsText": "India", "postedOn": "Today"},
            ]},
            {"total": 3, "jobPostings": [
                {"externalPath": "/job/three", "title": "Full Stack Engineer", "locationsText": "India", "postedOn": "Yesterday"},
            ]},
        ]
        requests = []

        def open_page(request, timeout):
            requests.append((json.loads(request.data), timeout))
            return io.BytesIO(json.dumps(responses[len(requests) - 1]).encode())

        with patch("jobpilot.load_json", return_value={"preferences": {"roles": ["Backend Developer"]}}), \
             patch("jobpilot.WORKDAY_PAGE_SIZE", 2), patch("jobpilot.WORKDAY_MAX_RESULTS_PER_QUERY", 4), \
             patch("jobpilot.urllib.request.urlopen", side_effect=open_page):
            jobs = list(jobpilot.workday(company))

        self.assertEqual([job.external_id for job in jobs], ["/job/one", "/job/two", "/job/three"])
        self.assertEqual([request[0]["offset"] for request in requests], [0, 2])
        self.assertTrue(all(request[0]["limit"] <= 2 for request in requests))

    def test_discovery_fetches_boards_concurrently_but_keeps_db_writes_serial(self):
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT / "tmp") as directory:
            root = Path(directory)
            companies = [{"company": f"Example {i}", "ats": "greenhouse"} for i in range(7)]
            lock = threading.Lock()
            active = maximum = 0

            def source(company):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.04)
                with lock:
                    active -= 1
                name = company["company"]
                yield jobpilot.Job(name, name, "Backend Engineer", "C# .NET", "India", "Fixture",
                                   f"https://jobs.example/{name.replace(' ', '-')}",
                                   f"https://jobs.example/{name.replace(' ', '-')}", "greenhouse")

            with patch("jobpilot.DB", root / "jobs.db"), patch("jobpilot.DATA", root / "data"), \
                 patch("jobpilot.load_json", side_effect=[companies, {}]), patch.dict(jobpilot.SOURCES, {"greenhouse": source}):
                jobpilot.cmd_discover(argparse.Namespace())
                db = jobpilot.connect()
                count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                db.close()

            self.assertGreater(maximum, 1)
            self.assertLessEqual(maximum, jobpilot.DISCOVERY_MAX_WORKERS)
            self.assertEqual(count, len(companies))

    def test_bounded_discovery_rotates_across_employer_boards(self):
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT / "tmp") as directory:
            root = Path(directory)
            companies = [{"company": f"Example {i}", "ats": "greenhouse"} for i in range(7)]
            visited = []

            def source(company):
                visited.append(company["company"])
                return []

            with patch("jobpilot.DB", root / "jobs.db"), patch("jobpilot.DATA", root / "data"), \
                 patch("jobpilot.load_json", return_value=companies), patch.dict(jobpilot.SOURCES, {"greenhouse": source}):
                for _ in range(3):
                    jobpilot.cmd_discover(argparse.Namespace(limit=3))
            self.assertEqual(visited, [f"Example {i}" for i in range(7)] + ["Example 0", "Example 1"])
            state = json.loads((root / "data" / "scheduler-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["employer_board_offset"], 2)

    def test_remote_country_restrictions_exclude_roles_that_do_not_accept_india(self):
        rules = {'maxExperienceYears': 4, 'indiaOnlyUnlessRemote': True}
        job = {'title':'Software Engineer', 'description':'Backend APIs and C#', 'location':'Remote - US'}
        self.assertEqual(jobpilot.eligible(job, rules), (False, 'Remote role is restricted to a region outside India'))
        job['location'] = 'Remote - India'
        self.assertEqual(jobpilot.eligible(job, rules), (True, 'Eligible'))
        job['location'] = 'Remote Worldwide'
        self.assertEqual(jobpilot.eligible(job, rules), (True, 'Eligible'))

    def test_experience_uses_candidate_years_and_separates_preferred_requirements(self):
        rules = {'maxExperienceYears': 4, 'indiaOnlyUnlessRemote': True}
        job = {'title':'.NET Developer', 'location':'Hyderabad, India', 'description':'0-4 years of experience'}
        self.assertEqual(jobpilot.eligible(job, rules), (True, 'Eligible'))
        job['description'] = '4 years of experience required'
        self.assertEqual(jobpilot.eligible(job, rules), (False, 'Requires at least 4 years; candidate profile has 3'))
        job['description'] = '4 years of experience preferred'
        self.assertEqual(jobpilot.eligible(job, rules), (True, 'Eligible'))
        job['description'] = '5 to 10 years of experience required'
        self.assertEqual(jobpilot.eligible(job, rules), (False, 'Requires at least 5 years; candidate profile has 3'))

    def test_specialist_title_is_excluded(self):
        for title in ("Software Engineer II Android", "Software Engineer III DevOps", "Software Engineer Sensors"):
            self.assertFalse(jobpilot.eligible({"title":title,"description":"","location":"India"}, {})[0])

    def test_sde_title_is_recognized_as_software_role(self):
        job={"title":"SDE (Automation & Quality Focus)","description":"Backend APIs, databases and test automation","location":"India"}
        self.assertTrue(jobpilot.eligible(job,{})[0])

    def test_unknown_tool_experience_requires_a_saved_fact(self):
        self.assertIsNone(jobpilot.question_answer("How many years of experience do you have with Temporal?")[0])
        self.assertIsNone(jobpilot.question_answer("Years of experience in Apache Flink")[0])

    def test_candidate_can_assume_affirmative_technical_skill_familiarity(self):
        original=jobpilot.load_json
        with patch("jobpilot.load_json",side_effect=lambda name: {"assume_technical_skill_experience":True} if name == "answers.json" else original(name)):
            self.assertEqual(jobpilot.question_answer("Do you have experience with Kubernetes?")[0],"Yes")
            self.assertEqual(jobpilot.question_answer("Have you worked with Apache Flink?")[0],"Yes")
            self.assertIsNone(jobpilot.question_answer("How many years of experience do you have with Kubernetes?")[0])
            self.assertIsNone(jobpilot.question_answer("Describe a project you built using Kubernetes")[0])

    def test_total_experience_is_derived_from_dated_employment(self):
        # The undated "3 years" in profile.json is no longer used; dated records are.
        import candidate_kb
        db = jobpilot.candidate_db()
        try: derived, _ = candidate_kb.total_experience(db)
        finally: db.close()
        answer = jobpilot.question_answer("Total years of experience")[0]
        self.assertIsNotNone(answer)
        self.assertLessEqual(float(answer), derived)
        self.assertGreater(float(answer), derived - 0.1)

    def test_experience_default_does_not_invent_project_details(self):
        self.assertIsNone(jobpilot.question_answer("Describe a project you built using Temporal")[0])

    def test_empty_question_does_not_pick_personal_answer(self):
        self.assertEqual(jobpilot.question_answer(""), (None, None))

    def test_conflicting_saved_notice_periods_answer_nothing(self):
        # answers.json says 60 days while profile.json says 30: keep the conflict out of live answers.
        original = jobpilot.load_json
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT / "tmp") as directory, patch("jobpilot.DATA", Path(directory)),                 patch("jobpilot.load_json", side_effect=lambda name: {"notice_period": "60 days"} if name == "answers.json" else original(name)):
            self.assertIsNone(jobpilot.question_answer("notice period")[0])
            import candidate_kb
            db = jobpilot.candidate_db()
            try: self.assertIn("notice_period_days", [c["key"] for c in candidate_kb.conflicts(db)])
            finally: db.close()

    def test_background_retry_does_not_recycle_structural_blockers(self):
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT / "tmp") as directory:
            with patch("jobpilot.DATA", Path(directory)), patch("jobpilot.DB", Path(directory) / "test.db"), patch("jobpilot.eligible", return_value=(True, "ok")):
                db = jobpilot.connect()
                for job_id, status, reason in [(1, 'SkippedBlocked', 'Missing required fields: Password'), (2, 'SkippedBlocked', 'No supported submit control found'), (3, 'FailedTemporary', 'Network unavailable')]:
                    db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats,eligibility_reason) VALUES(?, 'Test', 'Engineer', 'today', ?, ?, 'lever', ?)", (job_id, str(job_id), status, reason))
                    db.execute("INSERT INTO application_queue(job_id,created_at,status) VALUES(?,'today',?)", (job_id, status))
                db.commit(); db.close()
                jobpilot.cmd_retry(argparse.Namespace())
                db = jobpilot.connect()
                self.assertEqual([r[0] for r in db.execute('SELECT status FROM jobs ORDER BY id')], ['SkippedBlocked', 'SkippedBlocked', 'Queued'])
                db.close()

    def test_submission_timeout_is_not_retried(self):
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT / "tmp") as directory:
            root = Path(directory)
            (root/'resumes').mkdir()
            (root/'resumes'/'generic.pdf').write_bytes(b'%PDF-1.4\n%%EOF')
            with patch("jobpilot.DATA", root), patch("jobpilot.DB", root / "test.db"):
                db = jobpilot.connect()
                db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats) VALUES(1,'Test','Engineer','today','unique','Queued','lever')")
                db.execute("INSERT INTO application_queue(job_id,created_at) VALUES(1,'today')")
                db.commit(); db.close()
                def failed_apply(args):
                    connection = jobpilot.connect()
                    jobpilot.mark_submission_started(connection, args.job)
                    connection.close()
                    raise TimeoutError("Confirmation timed out")
                with patch("jobpilot._apply_job", side_effect=failed_apply):
                    with self.assertRaises(TimeoutError):
                        jobpilot.cmd_apply(argparse.Namespace(job=1, confirm_fill=True, unattended=True))
                jobpilot.cmd_retry(argparse.Namespace())
                db = jobpilot.connect()
                self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0], "ManualReview")
                attempt = db.execute("SELECT status,completed_at,step FROM application_attempts").fetchone()
                self.assertEqual(attempt[0], "ManualReview")
                self.assertTrue(attempt[1])
                self.assertEqual(attempt[2], "submit")
                self.assertEqual(db.execute("SELECT attempts FROM application_queue").fetchone()[0], 1)
                db.close()

    def test_option_matching_ignores_empty_placeholder(self):
        self.assertEqual(jobpilot.matching_option("Yes", ["", "Yes", "No"]), 1)

    def test_option_matching_prefers_exact_over_partial(self):
        self.assertEqual(jobpilot.matching_option("No", ["No sponsorship required", "No"]), 1)

    def test_option_matching_rejects_ambiguous_answers(self):
        self.assertIsNone(jobpilot.matching_option("Yes", ["Yes, immediately", "Yes, later"]))

    def test_manual_challenge_continues_only_after_resolution(self):
        with patch("jobpilot.blocking_challenge", side_effect=[True, False]), patch("builtins.input") as prompt:
            self.assertTrue(jobpilot.wait_for_manual_challenge(Mock(), False))
            prompt.assert_called_once()

    def test_unsolved_manual_challenge_remains_blocked(self):
        with patch("jobpilot.blocking_challenge", return_value=True), patch("builtins.input"):
            self.assertFalse(jobpilot.wait_for_manual_challenge(Mock(), False))

    def test_unattended_challenge_does_not_prompt(self):
        with patch("jobpilot.blocking_challenge", return_value=True), patch("builtins.input") as prompt:
            self.assertFalse(jobpilot.wait_for_manual_challenge(Mock(), True))
            prompt.assert_not_called()

    def test_common_question_matches(self):
        self.assertEqual(jobpilot.question_answer("Are you legally authorized to work in India?")[0], "Yes")
        self.assertEqual(jobpilot.question_answer("How soon can you join our company?")[0], "30 days")
        self.assertEqual(jobpilot.question_answer("What is your current compensation?")[0], "8 LPA")

    def test_fuzzy_bank_match_preserves_work_authorization_polarity_and_country(self):
        values={'answers.json':{'work_authorization_india':'Yes'},'profile.json':{},
                'question-bank.json':[{'key':'work_authorization_india','patterns':['authorized to work in india'],'autoFill':True}]}
        with patch('jobpilot.load_json',side_effect=values.__getitem__):
            self.assertEqual(jobpilot.question_answer('Are you legally authorized to work in India?')[0],'Yes')
            self.assertIsNone(jobpilot.question_answer('Are you not legally authorized to work in India?')[0])
            self.assertIsNone(jobpilot.question_answer('Are you legally authorized to work in the United States?')[0])

    def test_naukri_detection_and_search_url(self):
        self.assertEqual(jobpilot.ats_for("https://www.naukri.com/job-listings-example"), "naukri")
        url = jobpilot.naukri_search_url(".NET developer", "Bengaluru", 2)
        self.assertIn("jobs-in-india-2?", url)
        self.assertIn("k=.NET+developer", url)
        self.assertIn("l=Bengaluru", url)

    def test_naukri_direct_flow_is_enabled_only_with_confirmed_apply_adapter(self):
        settings = jobpilot.load_json("settings.json")
        from ats_forms import adapter_capabilities
        self.assertIn("naukri", settings["allowedAutoSubmitAts"])
        self.assertTrue(adapter_capabilities('naukri')['submit'])
        self.assertTrue(adapter_capabilities('naukri')['confirm'])
        self.assertTrue(supports_autosubmit('naukri'))

    def test_naukri_apply_control_skips_external_redirects(self):
        class Candidate:
            def __init__(self, href): self.href = href
            def get_attribute(self, name): return self.href if name == 'href' else None
            def is_visible(self): return True
            def is_enabled(self): return True
        class Locator:
            def __init__(self, candidates): self.candidates = candidates
            def filter(self, **kwargs): return self
            def all(self): return self.candidates
        class Page:
            url = 'https://www.naukri.com/job-listing'
            def locator(self, selector):
                return Locator([Candidate('https://employer.example/apply'), Candidate('/apply')])

        self.assertEqual(jobpilot.naukri_direct_apply_control(Page()).href, '/apply')

    def test_expired_queued_job_is_skipped_before_browser_launch(self):
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp') as directory:
            root=Path(directory)
            settings={'unattended':True,'autoSubmit':True,'allowedAutoSubmitAts':['greenhouse']}
            with patch('jobpilot.DATA',root), patch('jobpilot.DB',root/'test.db'), \
                 patch('jobpilot.load_json',side_effect=lambda name: settings if name=='settings.json' else {}), \
                 patch('jobpilot.promote_due_retries'), patch('jobpilot.eligible',return_value=(False,'Outside requested posting window')), \
                 patch('jobpilot.cmd_apply') as apply:
                db=jobpilot.connect()
                db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats,score) VALUES(1,'Old Co','Engineer','today','expired-1','Queued','greenhouse',90)")
                db.execute("INSERT INTO application_queue(job_id,created_at,status,score) VALUES(1,'today','Queued',90)")
                db.commit(); db.close()

                jobpilot._run_application_batch(argparse.Namespace(limit=5,dry_run=False,human_challenges=False))

                apply.assert_not_called()
                db=jobpilot.connect()
                self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=1').fetchone()[0],'Skipped')
                self.assertEqual(db.execute('SELECT status FROM application_queue WHERE job_id=1').fetchone()[0],'Skipped')
                db.close()

    def test_batch_preflights_unsupported_jobs_beyond_limit_before_first_attempt(self):
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp') as directory:
            root=Path(directory)
            settings={'unattended':True,'autoSubmit':True,'allowedAutoSubmitAts':['greenhouse']}
            with patch('jobpilot.DATA',root), patch('jobpilot.DB',root/'test.db'), \
                 patch('jobpilot.load_json',side_effect=lambda name: settings if name=='settings.json' else {}), \
                 patch('jobpilot.promote_due_retries'), patch('jobpilot.eligible',return_value=(True,'')), \
                 patch('jobpilot.cmd_apply') as apply:
                db=jobpilot.connect()
                db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats,score,apply_url) VALUES(1,'Ready Co','Engineer','today','ready-1','Queued','greenhouse',90,'https://boards.greenhouse.io/ready/1')")
                db.execute("INSERT INTO application_queue(job_id,created_at,status,score) VALUES(1,'today','Queued',90)")
                db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats,score,apply_url) VALUES(2,'Unsupported Co','Engineer','today','other-1','Queued','indeed',80,'https://indeed.com/viewjob/2')")
                db.execute("INSERT INTO application_queue(job_id,created_at,status,score) VALUES(2,'today','Queued',80)")
                db.commit(); db.close()

                jobpilot._run_application_batch(argparse.Namespace(limit=1,dry_run=False,human_challenges=False))

                apply.assert_called_once()
                self.assertEqual(apply.call_args.args[0].job,1)
                db=jobpilot.connect()
                self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=2').fetchone()[0],'Skipped')
                self.assertIn('Unsupported',db.execute('SELECT eligibility_reason FROM jobs WHERE id=2').fetchone()[0])
                db.close()

    def test_phase_timings_are_bounded_and_saved_without_page_data(self):
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp') as directory:
            manifest=Path(directory)/'attempt.json'
            manifest.write_text(json.dumps({'job_id':3,'attempt_id':4,'status':'Running'}),encoding='utf-8')
            args=argparse.Namespace(evidence_dir=Path(directory),phase_timings={'navigation':123.8,'submit':-2})
            jobpilot._save_attempt_timings(args)
            saved=json.loads(manifest.read_text(encoding='utf-8'))
            self.assertEqual(saved['phase_timings_ms'],{'navigation':123,'submit':0})
            self.assertNotIn('url',saved)

    def test_linkedin_apply_is_blocked_before_browser_attempt_even_in_dry_run(self):
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp') as directory:
            root=Path(directory)
            with patch('jobpilot.DATA',root),patch('jobpilot.DB',root/'test.db'):
                db=jobpilot.connect()
                db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats) VALUES(2,'Fixture','Engineer','today','linkedin-1','Queued','linkedin')")
                db.execute("INSERT INTO application_queue(job_id,created_at,status) VALUES(2,'today','Queued')")
                db.commit();db.close()
                args=argparse.Namespace(job=2,confirm_fill=True,unattended=True,dry_run=True)
                with self.assertRaisesRegex(SystemExit, 'LinkedIn is a manual handoff'):
                    jobpilot._run_application_attempt(args)
                db=jobpilot.connect()
                self.assertEqual(db.execute('SELECT COUNT(*) FROM application_attempts WHERE job_id=2').fetchone()[0],0)
                db.close()

    def test_passive_recaptcha_badge_is_not_a_blocker(self):
        page = Mock()
        page.locator.return_value.count.return_value = 0
        page.locator.return_value.inner_text.return_value = "Apply for this job"
        self.assertFalse(jobpilot.blocking_challenge(page))

    def test_priority_boundaries(self):
        self.assertEqual(jobpilot.priority(85), "P0")
        self.assertEqual(jobpilot.priority(75), "P1")
        self.assertEqual(jobpilot.priority(60), "P2")
        self.assertEqual(jobpilot.priority(50), "P3")

    def test_resume_selection(self):
        self.assertEqual(jobpilot.resume_for("C# ASP.NET Core REST API backend"), "backend.pdf")
        self.assertEqual(jobpilot.resume_for("Angular React full stack frontend"), "fullstack.pdf")
        self.assertEqual(jobpilot.resume_for("Kubernetes Azure DevOps SRE"), "platform-sre.pdf")

    def test_overseas_location_is_rejected_even_if_description_mentions_india(self):
        job = {"title": "Software Engineer", "description": "Our company also operates in India", "location": "Malaysia"}
        ok, reason = jobpilot.eligible(job, {"allowInternships": False, "maxExperienceYears": 5, "indiaOnlyUnlessRemote": True})
        self.assertFalse(ok)
        self.assertIn("India", reason)

    def test_india_location_is_allowed(self):
        job = {"title": "Software Engineer", "description": "Build APIs", "location": "Bengaluru, India"}
        self.assertTrue(jobpilot.eligible(job, {"allowInternships": False, "maxExperienceYears": 5, "indiaOnlyUnlessRemote": True})[0])

    def test_senior_role_is_rejected_for_current_profile(self):
        job = {"title": "Senior Software Engineer", "description": "Build APIs", "location": "India"}
        ok, reason = jobpilot.eligible(job, {"allowInternships": False, "maxExperienceYears": 5, "indiaOnlyUnlessRemote": True})
        self.assertFalse(ok)
        self.assertEqual(reason, "Seniority mismatch")

    def test_senior_title_within_saved_experience_range_is_allowed(self):
        job = {"title": "Sr. Software Engineer", "description": "3+ years of software development experience; backend APIs", "location": "Hyderabad, India"}
        self.assertTrue(jobpilot.eligible(job, {"maxExperienceYears": 3, "indiaOnlyUnlessRemote": True})[0])

    def test_java_engineer_is_recognized_as_a_software_role(self):
        job = {"title": "Java Engineer (Back-End & Microservices)", "description": "Spring Boot and REST APIs", "location": "Bangalore, India"}
        self.assertTrue(jobpilot.eligible(job, {"maxExperienceYears": 3, "indiaOnlyUnlessRemote": True})[0])

    def test_matcher_excludes_lead_product_and_support_titles(self):
        for title in ("Lead, Platform Engineering", "Product Manager, Developer Platform", "Developer Support Engineer"):
            with self.subTest(title=title):
                ok, reason = jobpilot.eligible({"title":title,"description":"C# .NET SQL backend","location":"Bengaluru, India"}, {})
                self.assertFalse(ok)
                self.assertTrue("role" in reason.casefold() or "seniority" in reason.casefold())

    def test_matcher_detects_mandatory_experience_even_with_optional_tool_years(self):
        job={"title":"Software Engineer","description":"Requires 5+ years of software development experience. 1+ years of Docker is a bonus.","location":"India"}
        ok, reason=jobpilot.eligible(job,{"maxExperienceYears":3,"indiaOnlyUnlessRemote":True})
        self.assertFalse(ok)
        self.assertIn("mandatory years",reason)


if __name__ == "__main__":
    unittest.main()
