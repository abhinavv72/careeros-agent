"""Offline browser fixtures. Enable with JOBPILOT_BROWSER_TESTS=1."""
import os
import unittest
import argparse
import tempfile
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import patch
from playwright.sync_api import sync_playwright, BrowserType
import ats_forms
import jobpilot
from application_support import form_fingerprint, wait_for_form_change, wait_for_upload
import json


@unittest.skipUnless(os.environ.get("JOBPILOT_BROWSER_TESTS") == "1", "Offline browser tests are opt-in")
class AtsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(channel="chrome", headless=True, timeout=15000)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def test_conditional_questions_and_validation(self):
        self.page.set_content('''<label>Email<input type=email name=email required></label>
            <label>Notice period<input name=notice required></label>
            <label>Legal declaration<input type=checkbox required></label>
            <input required style="display:none">
            <button type=submit>Next</button>''')
        jobpilot.fill_known_fields(self.page, {"email": "candidate@example.com"})
        self.assertEqual(self.page.locator("[name=notice]").input_value(), "30 days")
        missing = ats_forms.unresolved_fields(self.page)
        self.assertEqual([f["question"] for f in missing], ["Legal declaration"])
        self.assertIsNone(ats_forms.submission_control(self.page, "lever"))
        self.page.locator("[name=email]").fill("invalid")
        self.assertTrue(any(f["invalid"] for f in ats_forms.unresolved_fields(self.page)))
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<label>Current location<input name=city required></label>')")
        jobpilot.fill_known_fields(self.page, {"email": "candidate@example.com", "city": "Hyderabad"})
        self.assertEqual(self.page.locator("[name=city]").input_value(), "Hyderabad")

    def test_dropdown_placeholder_and_final_submit(self):
        self.page.set_content('''<div class=field>Require sponsorship
            <select required><option value="">Choose</option><option value=y>Yes</option><option value=n>No</option></select>
            </div><button type=submit>Submit application</button>''')
        jobpilot.fill_known_fields(self.page, {})
        self.assertEqual(self.page.locator("select").input_value(), "n")
        self.assertEqual(ats_forms.unresolved_fields(self.page), [])
        self.assertIsNotNone(ats_forms.submission_control(self.page, "lever"))

    def test_lever_nested_question_labels(self):
        self.page.set_content('''<li class="application-question custom-question"><div>
            <div class="application-label"><div class="text">How quickly can you join us if shortlisted?</div></div>
            <div class="application-field"><textarea name="cards[x][field0]" required></textarea></div>
            </div></li>''')
        self.assertIn("How quickly", ats_forms.unresolved_fields(self.page)[0]["question"])
        jobpilot.fill_known_fields(self.page, {})
        self.assertEqual(self.page.locator("textarea").input_value(), "30 days")
        self.assertEqual(ats_forms.unresolved_fields(self.page), [])

    def test_navigation_is_scoped_to_application(self):
        self.page.set_content('<button>Next</button><form><button type=button>Continue</button></form>')
        self.assertEqual(ats_forms.next_application_control(self.page).inner_text(), 'Continue')
        self.page.set_content('<button>Next</button>')
        self.assertIsNone(ats_forms.next_application_control(self.page))

    def test_form_wait_handles_page_and_changed_labels(self):
        self.page.set_content('<label id=label>First question<input name=answer></label>')
        before=form_fingerprint(self.page)
        self.page.evaluate("document.querySelector('#label').firstChild.textContent='Second question'")
        wait_for_form_change(self.page,before,timeout=1000)

    def test_upload_waits_for_progress_to_finish(self):
        self.page.set_content('<input type=file><div role=progressbar>Uploading</div>')
        self.page.evaluate("setTimeout(()=>document.querySelector('[role=progressbar]').remove(),100)")
        wait_for_upload(self.page,timeout=1000)
        self.assertEqual(self.page.locator('[role=progressbar]').count(),0)

    def test_human_challenge_state_is_linked_to_the_current_attempt(self):
        self.page.set_content('''<div>Verify that you are human</div>
            <script>setTimeout(()=>document.body.innerHTML='<div>Application form</div>',150)</script>''')
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp') as directory:
            root=Path(directory)
            signal=root/'run.continue'
            with patch('jobpilot.DATA',root),patch.dict(os.environ,{'JOBPILOT_DASHBOARD':'1','JOBPILOT_RUN_ID':'run-1',
                'JOBPILOT_ATTEMPT_ID':'attempt-2','JOBPILOT_CURRENT_JOB':'job-9','JOBPILOT_CURRENT_COMPANY':'Fixture Co',
                'JOBPILOT_APPLICATION_PHASE':'before_submit','JOBPILOT_CONTINUE_FILE':str(signal)},clear=False):
                jobpilot.wait_for_user(self.page,'Complete fixture verification')
            challenge=json.loads((root/'captcha_challenge.json').read_text(encoding='utf-8'))
            self.assertEqual(challenge['status'],'Resolved')
            self.assertEqual(challenge['run_id'],'run-1')
            self.assertEqual(challenge['job_id'],'job-9')
            self.assertEqual(challenge['attempt_id'],'attempt-2')

    def test_verified_contact_overwrites_incorrect_resume_parsing(self):
        self.page.set_content('<label>Email<input name=email type=email value="incorrect@example.com"></label>')
        jobpilot.fill_known_fields(self.page,{'email':'verified@example.com'})
        self.assertEqual(self.page.locator('input').input_value(),'verified@example.com')

    def test_phone_field_is_not_misfilled_from_a_shared_country_and_name_wrapper(self):
        self.page.set_content('''<div class="application--questions">
            <div><label for="first_name">First Name</label><input id="first_name" aria-label="First Name"></div>
            <fieldset class="phone-input"><div class="phone-input__country"><label>Country</label><button>+91</button></div>
                <label for="phone">Phone</label><input id="phone" type="tel" aria-label="Phone"></fieldset>
            </div>''')
        values={'first name':'Sample','phone':'+1 555 0100'}
        jobpilot.fill_known_fields(self.page,values)
        self.assertEqual(self.page.locator('#phone').input_value(),'5550100')
        self.assertEqual(jobpilot.contact_field_errors(self.page,values),[])
        self.page.locator('#phone').fill('Sample')
        self.assertEqual(jobpilot.contact_field_errors(self.page,values),['Phone'])

    def test_unidentified_email_input_uses_candidate_email_only_for_identity_label(self):
        self.page.set_content('<label>Email<input type=email required></label><label>Referrer email<input type=email required></label>')
        jobpilot.fill_known_fields(self.page,{'email':'verified@example.com'})
        self.assertEqual(self.page.locator('input').first.input_value(),'verified@example.com')
        self.assertEqual(self.page.locator('input').last.input_value(),'')

    def pipeline_fixture(self, html, dry=False, response_status=200, ats='lever'):
        original=BrowserType.launch_persistent_context
        def launch(browser_type,*args,**kwargs):
            context=original(browser_type,*args,**kwargs)
            context.route('**/*',lambda route:route.fulfill(body=html,content_type='text/html',status=response_status))
            return context
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT/'tmp') as directory:
            root=Path(directory)
            (root/'resumes').mkdir()
            (root/'resumes'/'generic.pdf').write_bytes(b'%PDF-1.4\n%%EOF')
            with patch('jobpilot.DATA',root),patch('jobpilot.DB',root/'test.db'),patch('jobpilot.APPLICATION_PROFILE',root/'browser'),patch.object(BrowserType,'launch_persistent_context',launch),patch('playwright.sync_api.sync_playwright',return_value=nullcontext(self.runtime)),patch('jobpilot.eligible',return_value=(True,'Fixture')):
                db=jobpilot.connect()
                url='https://boards.greenhouse.io/fixture/jobs/1' if ats=='greenhouse' else 'https://jobs.lever.co/fixture/apply'
                db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats,apply_url,resume) VALUES(1,'Fixture','Software Engineer','today','unique','Queued',?,?, 'generic.pdf')",(ats,url))
                db.execute("INSERT INTO application_queue(job_id,created_at) VALUES(1,'today')")
                db.commit()
                try:
                    jobpilot.cmd_apply(argparse.Namespace(job=1,confirm_fill=True,unattended=True,dry_run=dry))
                    status=db.execute('SELECT status FROM jobs').fetchone()[0]
                    folder=root/'screenshots'/'job-1'/'attempt-1'
                    receipt=json.loads((folder/'receipt.json').read_text()) if (folder/'receipt.json').is_file() else None
                    questions=json.loads((folder/'questions.json').read_text()) if (folder/'questions.json').is_file() else None
                    return status,receipt,questions
                finally:db.close()

    def test_submission_pipeline_saves_a_receipt(self):
        html='''<label>Full name<input name=name required></label><label>Email<input name=email type=email required></label>
            <input type=file name=resume><button type=submit onclick="document.body.innerHTML='Thank you for applying'">Submit application</button>'''
        status,receipt,questions=self.pipeline_fixture(html)
        self.assertEqual(status,'Applied')
        self.assertEqual(receipt['confirmation_text'],'Thank you for applying')
        self.assertEqual(receipt['resume']['filename'],'generic.pdf')

    def test_greenhouse_application_pipeline_saves_a_receipt(self):
        html='''<form><label>First name<input id=first_name required></label><label>Last name<input id=last_name required></label>
            <label>Email<input type=email required></label><input type=file id=resume name=resume>
            <button type=submit id=submit_app onclick="event.preventDefault();document.body.innerHTML='Thank you for applying'">Submit application</button></form>'''
        status,receipt,questions=self.pipeline_fixture(html,ats='greenhouse')
        self.assertEqual(status,'Applied')
        self.assertEqual(receipt['confirmation_text'],'Thank you for applying')
        self.assertEqual(receipt['resume']['filename'],'generic.pdf')
        self.assertEqual(questions,[])

    def test_resume_on_second_application_step_is_uploaded_before_submit(self):
        html='''<form><label>Full name<input name=name required></label>
            <label>Email<input name=email type=email required></label><button type=button id=next>Next</button></form>
            <script>document.querySelector("#next").onclick=()=>{
              document.body.innerHTML="<form><label>Email<input name=email type=email required></label><label>Resume<input type=file name=resume required></label><button type=submit id=submit>Submit application</button></form>";
              document.querySelector("#submit").onclick=e=>{e.preventDefault();document.body.innerHTML="Thank you for applying"};
            }</script>
            '''
        status,receipt,questions=self.pipeline_fixture(html)
        self.assertEqual(status,'Applied')
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt['resume']['filename'],'generic.pdf')
        self.assertEqual(questions,[])

    def test_resume_input_can_disappear_after_successful_upload(self):
        html='''<label>Full name<input name=name required></label><label>Email<input name=email type=email required></label>
            <input type=file name=resume onchange="this.remove()"><button type=submit onclick="document.body.innerHTML='Thank you for applying'">Submit application</button>'''
        status,receipt,questions=self.pipeline_fixture(html)
        self.assertEqual(status,'Applied')
        self.assertIsNotNone(receipt)
        self.assertEqual(questions,[])

    def test_bamboohr_opens_application_instead_of_treating_entry_as_submit(self):
        self.page.set_content('''<button onclick="document.body.innerHTML='<label>Email<input type=email></label><input type=file name=resume><button type=button>Submit Application</button>'">Apply for This Job</button>''')
        self.assertIsNone(ats_forms.submission_control(self.page,'bamboohr'))
        surface=ats_forms.prepare_application(self.page,'bamboohr')
        self.assertEqual(surface.locator('input[type=email]').count(),1)
        self.assertEqual(ats_forms.submission_control(surface,'bamboohr').inner_text(),'Submit Application')

    def test_delayed_application_entry_does_not_accept_search_or_hidden_fields(self):
        self.page.set_content('''<input type=search placeholder="Search jobs"><input type=email hidden>
            <script>setTimeout(()=>{
                const button=document.createElement('button');button.textContent='Apply for This Job';
                button.onclick=()=>document.body.innerHTML='<form><label>Email<input type=email></label><input type=file name=resume><button type=button>Submit your application</button></form>';
                document.body.append(button);
            },250)</script>''')
        surface=ats_forms.prepare_application(self.page,'bamboohr')
        self.assertEqual(surface.locator('form input[type=email]').count(),1)
        self.assertIsNotNone(ats_forms.submission_control(surface,'bamboohr'))

    def test_generic_application_entry_works_without_an_ats_specific_adapter(self):
        self.page.set_content('''<button onclick="document.body.innerHTML='<form><input type=email><div role=button aria-label=Submit&#32;application>→</div></form>'">Start application</button>''')
        surface=ats_forms.prepare_application(self.page,'generic')
        self.assertIsNotNone(ats_forms.submission_control(surface,'generic'))

    def test_submission_control_refuses_disabled_and_ambiguous_actions(self):
        self.page.set_content('<form><input type=email><button disabled>Submit application</button><button>Save</button></form>')
        self.assertIsNone(ats_forms.submission_control(self.page,'generic'))
        self.assertTrue(any(row['disabled'] for row in ats_forms.submit_diagnostics(self.page)))
        self.page.set_content('<form><input type=email><button>Submit application</button><button>Send application</button></form>')
        self.assertIsNone(ats_forms.submission_control(self.page,'generic'))

    def test_stray_alert_email_does_not_hide_the_apply_entry(self):
        # Stripe-style listing: a job-alert email box plus an "Apply for this role" link.
        self.page.set_content('''<input type=email placeholder="Get job alerts">
            <a href="#" onclick="document.body.innerHTML='<form><label>First name<input name=first_name></label><label>Email<input type=email name=email></label><input type=file name=resume><button type=submit>Submit application</button></form>';return false">Apply for this role</a>''')
        surface=ats_forms.prepare_application(self.page,'greenhouse')
        self.assertGreaterEqual(surface.locator(ats_forms.APPLICATION_FIELDS).count(),2)
        self.assertEqual(ats_forms.submission_control(surface,'greenhouse').inner_text().strip(),'Submit application')

    def test_in_form_apply_button_is_never_clicked_as_an_entry(self):
        # One visible field plus an in-form "Apply" may be the final submit: never press it.
        self.page.set_content('<form><input type=email name=email><button type=button onclick="window.pressed=true">Apply</button></form>')
        ats_forms.prepare_application(self.page,'greenhouse')
        self.assertFalse(self.page.evaluate('!!window.pressed'))

    def test_embedded_greenhouse_form_behind_a_page_tab_is_opened(self):
        # Airbnb-style: Greenhouse iframe sits in a hidden "Application" panel until the
        # page-level "Apply Now" button is pressed.
        self.page.route('https://boards.greenhouse.io/**',lambda route:route.fulfill(content_type='text/html',body='''
            <form><label>First name<input id=first_name name=first_name></label><label>Email<input type=email name=email></label>
            <input type=file name=resume><button type=submit>Submit application</button></form>'''))
        self.page.route('https://careers.example.test/**',lambda route:route.fulfill(content_type='text/html',body='''
            <button onclick="document.getElementById('panel').style.display='block'">Apply Now</button>
            <div id=panel style="display:none"><iframe src="https://boards.greenhouse.io/embed/job_app?for=example" width=800 height=600></iframe></div>'''))
        self.page.goto('https://careers.example.test/positions/1')
        self.page.wait_for_timeout(500)
        surface=ats_forms.prepare_application(self.page,'greenhouse')
        self.assertIsNot(surface,self.page)
        self.assertIsNotNone(ats_forms.submission_control(surface,'greenhouse'))

    def test_cloudflare_security_check_is_a_blocking_challenge(self):
        self.page.set_content('<h1>Security Check</h1><p>Support ID: abc</p>')
        self.assertTrue(jobpilot.blocking_challenge(self.page))
        self.page.set_content('<form><label>Email<input type=email></label><button>Submit application</button></form>')
        self.assertFalse(jobpilot.blocking_challenge(self.page))

    KEYSTROKE_CODE_FORM = '''<form onsubmit="return false"><p>A verification code was sent to you. Enter the 4-character code.</p>
        <div id=boxes></div><button type=button id=sub>Submit application</button></form>
        <script>const got=[];for(let i=0;i<4;i++){const el=document.createElement('input');el.id='security-input-'+i;el.maxLength=1;
        el.addEventListener('keydown',e=>{if(e.key.length===1)got[i]=e.key;});document.getElementById('boxes').append(el);}
        document.getElementById('sub').onclick=()=>{document.body.innerHTML=got.join('')==='AB12'
          ?'<h1>Thank you for applying</h1>':'<p>There was an error processing your application. Please try again.</p>';};</script>'''

    def test_verification_code_is_typed_so_keystroke_widgets_receive_it(self):
        # Widgets like this ignore a directly set value; only real key events reach them.
        self.page.set_content(self.KEYSTROKE_CODE_FORM)
        submit=self.page.locator('#sub')
        with patch.dict('os.environ',{},clear=False), patch('gmail_verifier.fetch_latest_verification_code',return_value='AB12'):
            os.environ.pop('JOBPILOT_CONTINUE_FILE',None); os.environ.pop('JOBPILOT_CURRENT_JOB',None)
            jobpilot.complete_email_verification(self.page,self.page,submit)
        self.assertIn('Thank you for applying',self.page.locator('body').inner_text())

    def test_rejected_code_resubmission_is_reported_and_returns(self):
        self.page.set_content(self.KEYSTROKE_CODE_FORM)
        submit=self.page.locator('#sub')
        import io, contextlib
        out=io.StringIO()
        with patch('gmail_verifier.fetch_latest_verification_code',return_value='ZZ99'), contextlib.redirect_stdout(out):
            os.environ.pop('JOBPILOT_CONTINUE_FILE',None); os.environ.pop('JOBPILOT_CURRENT_JOB',None)
            jobpilot.complete_email_verification(self.page,self.page,submit)
        self.assertIn('Employer rejected the resubmission after the code',out.getvalue())
        self.assertNotIn('ZZ99',out.getvalue())

    def test_screenshot_masks_code_boxes_inside_an_employer_iframe(self):
        self.page.route('https://boards.greenhouse.io/**',lambda route:route.fulfill(content_type='text/html',body='''
            <p>Enter the verification code</p>''' + ''.join(f'<input id=security-input-{i} value="{c}" style="font-size:40px;width:60px">' for i,c in enumerate('WXYZ'))))
        self.page.set_content('<iframe src="https://boards.greenhouse.io/embed/job_app" width=600 height=200></iframe>')
        self.page.frame_locator('iframe').locator('#security-input-0').wait_for()
        with tempfile.TemporaryDirectory() as tmp:
            masked=Path(tmp)/'masked.png'; plain=Path(tmp)/'plain.png'
            jobpilot.safe_screenshot(self.page,masked)
            self.page.screenshot(path=str(plain))
            self.assertTrue(masked.exists())
            self.assertNotEqual(masked.read_bytes(),plain.read_bytes())

    def test_explicit_current_residence_is_not_assumed_from_relocation(self):
        text='This role requires being located in Croatia. Please confirm that you are currently located in Croatia.'
        self.assertIsNotNone(ats_forms.residence_requirement(text,{'personal':{'country':'India'}}))
        self.assertIsNone(ats_forms.residence_requirement(text,{'personal':{'country':'Croatia'}}))
        self.assertIsNone(ats_forms.residence_requirement('Are you willing to relocate to Croatia?',{'personal':{'country':'India'}}))

    def test_delayed_submit_control_is_waited_for_without_clicking(self):
        self.page.set_content('''<form><input type=email><script>window.submissions=0;setTimeout(()=>{
            const button=document.createElement('button');button.type='button';button.textContent='Submit Application';
            button.onclick=()=>window.submissions++;document.querySelector('form').append(button);
        },200)</script></form>''')
        self.assertIsNotNone(ats_forms.wait_for_submission_control(self.page,'generic',timeout=2000))
        self.assertEqual(self.page.evaluate('window.submissions'),0)

    def test_dashboard_reconnects_and_never_replays_a_start_request(self):
        html=(jobpilot.ROOT/'dashboard.html').read_text(encoding='utf-8')
        calls={'health':0,'runs':0}
        def route(request):
            path=request.request.url.split('8765',1)[-1]
            if path=='/':request.fulfill(body=html,content_type='text/html');return
            if path=='/api/health':
                calls['health']+=1
                if calls['health']<=2:request.abort();return
                value={'ready':True,'restart_required':False}
            elif path=='/api/state':value={'active':None,'counts':[],'runs':[],'capabilities':{}}
            elif path.startswith('/api/jobs'):value=[]
            elif path=='/api/captcha-status':value={'status':'Clear'}
            elif path=='/api/runs':
                calls['runs']+=1;request.abort();return
            else:value={}
            request.fulfill(body=json.dumps(value),content_type='application/json')
        self.page.route('http://127.0.0.1:8765/**',route)
        self.page.goto('http://127.0.0.1:8765/')
        self.page.get_by_text('Disconnected',exact=True).wait_for(state='visible',timeout=3000)
        self.page.get_by_text('Ready',exact=True).wait_for(state='visible',timeout=10000)
        self.assertEqual(self.page.locator('#message').inner_text(),'')
        result=self.page.evaluate("async () => {try {await api('runs','POST',{});return 'unexpected'}catch(e){return e.message}}")
        self.assertIn('may have started',result)
        self.assertEqual(calls['runs'],1)

    def test_closed_vacancy_is_skipped_before_form_filling(self):
        status,receipt,questions=self.pipeline_fixture('<p>This position has been filled.</p>',response_status=410)
        self.assertEqual(status,'Skipped')
        self.assertIsNone(receipt)
        self.assertIsNone(questions)

    def test_multi_page_pipeline_waits_for_next_form(self):
        html='''<form><label>Email<input name=email required type=email></label><input type=file name=resume>
            <button type=button onclick="document.querySelector('form').innerHTML='<label>First name<input name=firstname required></label><button type=submit>Submit application</button>'">Next</button></form>'''
        status,receipt,questions=self.pipeline_fixture(html,dry=True)
        self.assertEqual(status,'Queued')
        self.assertIsNone(receipt)
        self.assertEqual(questions,[])

    def test_react_select_reports_selected_value_not_hidden_required_input(self):
        self.page.set_content('''<div class=field-wrapper><div class=select__container>
            <label id=country-label for=country>Country</label><span class=select__single-value>India</span>
            <input id=country role=combobox aria-required=true aria-labelledby=country-label>
            <input required aria-hidden=true tabindex=-1></div></div>''')
        self.assertEqual(ats_forms.unresolved_fields(self.page), [])

    def test_embedded_greenhouse_form_and_resume_selector(self):
        self.page.route("https://boards.greenhouse.io/**", lambda route: route.fulfill(body='''
            <label>First name<input id=first_name required></label>
            <input type=file id=cover_letter><input type=file id=resume>''', content_type="text/html"))
        self.page.set_content('<iframe src="https://boards.greenhouse.io/test"></iframe>')
        self.page.frame_locator("iframe").locator("#first_name").wait_for(state="visible")
        surface = ats_forms.prepare_application(self.page, "greenhouse")
        jobpilot.fill_known_fields(surface, {"first name": "Candidate"})
        self.assertEqual(surface.locator("#first_name").input_value(), "Candidate")
        self.assertEqual(surface.locator(ats_forms.ADAPTERS["greenhouse"].resume).get_attribute("id"), "resume")

    def test_late_greenhouse_iframe_is_recognized(self):
        self.page.route('https://boards.greenhouse.io/**',lambda route:route.fulfill(body='<label>First name<input id=first_name></label>',content_type='text/html'))
        self.page.set_content('''<input type=search><script>setTimeout(()=>{
            const frame=document.createElement('iframe');frame.src='https://boards.greenhouse.io/fixture';document.body.append(frame);
        },200)</script>''')
        surface=ats_forms.prepare_application(self.page,'greenhouse')
        self.assertEqual(surface.locator('#first_name').count(),1)

    def test_full_fill_only_run_saves_trace_without_submission(self):
        html = '''<label>Full name<input name=name required></label>
            <label>Email<input name=email type=email required></label>
            <input type=file name=resume><button type=submit onclick="document.body.innerHTML='APPLICATION SENT'">Submit application</button>'''
        original = BrowserType.launch_persistent_context
        def launch(browser_type, *args, **kwargs):
            context = original(browser_type, *args, **kwargs)
            context.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html"))
            return context
        with tempfile.TemporaryDirectory(dir=jobpilot.ROOT / "tmp") as directory:
            root = Path(directory)
            (root / "resumes").mkdir()
            (root / "resumes" / "generic.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
            with patch("jobpilot.DATA", root), patch("jobpilot.DB", root / "test.db"), patch("jobpilot.APPLICATION_PROFILE", root / "browser"), patch.object(BrowserType, "launch_persistent_context", launch), patch("playwright.sync_api.sync_playwright", return_value=nullcontext(self.runtime)):
                db = jobpilot.connect()
                db.execute("INSERT INTO jobs(id,company,title,discovered_at,fingerprint,status,ats,apply_url,resume) VALUES(1,'Fixture','Engineer','today','unique','Queued','lever','https://jobs.lever.co/fixture/apply','generic.pdf')")
                db.execute("INSERT INTO application_queue(job_id,created_at) VALUES(1,'today')")
                db.commit(); db.close()
                jobpilot.cmd_apply(argparse.Namespace(job=1, confirm_fill=True, unattended=True, dry_run=True))
                db = jobpilot.connect()
                self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0], "Queued")
                self.assertEqual(db.execute("SELECT status FROM application_attempts").fetchone()[0], "DryRun")
                db.close()
                for name in ("job-1-trace.zip", "job-1-filled.png", "job-1-questions.json"):
                    self.assertTrue((root / "screenshots" / name).is_file(), name)


class PostingClosedTests(unittest.TestCase):
    """Greenhouse sends removed postings to the board index; that is a closed job, not a form failure."""
    class _Page:
        def __init__(self, url): self.url = url

    def test_greenhouse_error_redirect_is_closed(self):
        page = self._Page('https://job-boards.greenhouse.io/globalhealthcareexchangeinc?error=true')
        self.assertTrue(ats_forms.posting_closed(page, 'https://job-boards.greenhouse.io/globalhealthcareexchangeinc/jobs/4733072005'))

    def test_greenhouse_redirect_to_board_index_is_closed(self):
        page = self._Page('https://job-boards.greenhouse.io/acme')
        self.assertTrue(ats_forms.posting_closed(page, 'https://job-boards.greenhouse.io/acme/jobs/123'))

    def test_live_greenhouse_job_and_other_hosts_are_not_closed(self):
        self.assertFalse(ats_forms.posting_closed(self._Page('https://job-boards.greenhouse.io/acme/jobs/123'), 'https://job-boards.greenhouse.io/acme/jobs/123'))
        self.assertFalse(ats_forms.posting_closed(self._Page('https://stripe.com/careers/listing/x/8130876'), 'https://stripe.com/jobs/search?gh_jid=8130876'))
