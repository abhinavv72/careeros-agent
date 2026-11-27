"""Phase 1: answers come only from verified facts, dated records and evidence cards."""
from datetime import date
from pathlib import Path
import json
import tempfile
import unittest

import answer_engine as ae
import candidate_kb as kb

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 22)

PROFILE = {'personal': {'firstName': 'Asha', 'lastName': 'Rao', 'email': 'asha@example.test', 'phone': '+91 9000000000',
                        'city': 'Hyderabad', 'state': 'Telangana', 'country': 'India'},
           'experience': {'years': 3, 'noticePeriodDays': 30},
           'preferences': {'locations': ['Hyderabad', 'Bengaluru', 'Remote']}}
ANSWERS = {'work_authorization_india': 'Yes', 'require_sponsorship': 'No', 'current_compensation': '8 LPA',
           'expected_compensation': '15 LPA', 'default_tool_experience_years': '3', 'assume_technical_skill_experience': True,
           'exact_questions': {'are you hispanic latino': 'Yes', 'are you immediate joiner': 'Yes', 'age': 'summary'}}
SEED = {'source': 'resume:test', 'employment_history_complete': True,
        'employment': [{'id': 'acme', 'employer': 'Acme', 'title': 'Engineer', 'type': 'full_time', 'start': '2024-01', 'end': None},
                       {'id': 'intern1', 'employer': 'Beta', 'title': 'Intern', 'type': 'internship', 'start': '2023-06', 'end': '2023-07'},
                       {'id': 'intern2', 'employer': 'Gamma', 'title': 'Intern', 'type': 'internship', 'start': '2023-07', 'end': '2023-11'}],
        'education': [{'id': 'uni', 'institution': 'Test University', 'degree': 'B.Tech', 'grade': 'CGPA 9.0/10', 'start': '2020-08', 'end': '2024-07'}],
        'skills_claimed': ['C#', 'Python', 'SQL'],
        'evidence_cards': [{'id': 'latency', 'kind': 'employment', 'employment_id': 'acme', 'claim': 'I cut API latency from 1300 ms to 40 ms.',
                            'skills': ['C#', 'SQL'], 'tags': ['performance', 'challenging']}]}


class AnswerEngineTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.db = kb.connect(Path(self.tmp.name) / 'candidate.db')
        kb.import_legacy(self.db, PROFILE, ANSWERS, SEED)
        self.ctx = ae.Context(job_id=1, employer='Okta', job_country='IN', location='Bengaluru, India', source='greenhouse',
                              ats='greenhouse', today=TODAY)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def ask(self, question, options=(), required=True, kind='text', ctx=None):
        return ae.resolve(ae.Field(question, list(options), required, kind), ctx or self.ctx, self.db)

    def test_every_fill_cites_live_evidence(self):
        for question, options in [('First name', []), ('Are you legally authorized to work in India?', ['Yes', 'No']),
                                  ('How many years of professional experience do you have?', []),
                                  ('Describe a challenging problem you solved', [])]:
            result = self.ask(question, options)
            self.assertEqual(result.action, ae.FILL, question)
            self.assertTrue(result.evidence, question)

    def test_legacy_guesses_never_answer(self):
        self.assertNotEqual(self.ask('Are you Hispanic/Latino?', ['Yes', 'No']).answer, 'Yes')
        self.assertEqual(self.ask('Are you an immediate joiner?', ['Yes', 'No']).answer, 'No')  # 30-day notice
        self.assertEqual(len([f for f in kb.legacy_facts(self.db) if 'hispanic' in f.key]), 1)

    def test_authorization_and_sponsorship_are_country_scoped(self):
        self.assertEqual(self.ask('Do you require visa sponsorship?', ['Yes', 'No']).answer, 'No')
        us = ae.Context(job_id=2, employer='X', job_country='US', today=TODAY)
        self.assertEqual(self.ask('Do you require visa sponsorship?', ['Yes', 'No'], ctx=us).action, ae.BLOCK)
        self.assertEqual(self.ask('Are you authorized to work in the United Kingdom?', ['Yes', 'No']).action, ae.BLOCK)
        unknown = ae.Context(job_id=3, job_country=None, today=TODAY)
        self.assertEqual(self.ask('Are you legally authorized to work in the country where this job is located?', ['Yes', 'No'], ctx=unknown).action, ae.BLOCK)
        self.assertEqual(self.ask('Are you NOT legally authorized to work in India?', ['Yes', 'No']).answer, 'No')

    def test_experience_is_derived_from_dates_without_double_counting(self):
        years, _ = kb.total_experience(self.db, TODAY, include_internships=True)
        # Jun–Jul and Jul–Nov 2023 overlap in July: 6 months, not 7.
        self.assertAlmostEqual(years - kb.total_experience(self.db, TODAY)[0], 183 / 365.25, places=2)
        self.assertEqual(self.ask('How many years of professional experience do you have?').answer, '2.8')
        self.assertEqual(self.ask('Years of experience', ['0-1', '1-3', '3-5', '5+']).answer, '1-3')

    def test_skill_years_need_confirmed_usage_not_a_shared_default(self):
        self.assertEqual(self.ask('How many years of experience do you have with Python?').action, ae.BLOCK)
        self.assertEqual(self.ask('How many years of experience do you have with Kubernetes?').action, ae.BLOCK)
        kb.confirm_skill(self.db, 'Python', '2025-01', employment_id='acme')
        self.assertEqual(self.ask('How many years of experience do you have with Python?').answer, '1.8')

    def test_skill_yes_no_needs_evidence(self):
        self.assertEqual(self.ask('Do you have experience with C#?', ['Yes', 'No']).answer, 'Yes')
        self.assertEqual(self.ask('Do you have experience with Kubernetes?', ['Yes', 'No']).action, ae.BLOCK)

    def test_prior_employment_uses_history(self):
        self.assertEqual(self.ask('Have you been employed by Okta in the past?', ['Yes', 'No']).answer, 'No')
        acme = ae.Context(job_id=4, employer='Acme Corp', job_country='IN', today=TODAY)
        self.assertEqual(self.ask('Have you ever worked at Acme before?', ['Yes', 'No'], ctx=acme).answer, 'Yes')

    def test_consent_needs_standing_consent_and_own_words_always_blocks(self):
        self.assertEqual(self.ask('I agree to the privacy policy', kind='checkbox').action, ae.BLOCK)
        self.assertEqual(self.ask('I agree to the privacy policy', kind='checkbox', required=False).action, ae.OMIT)
        kb.put_fact(self.db, 'consent.privacy_notice', 'granted', source='candidate', verification=kb.USER_CONFIRMED)
        self.assertEqual(self.ask('I agree to the privacy policy', kind='checkbox').answer, 'Yes')
        self.assertEqual(self.ask('I agree to use only my own words; AI generated content disqualifies', ['Yes', 'No']).action, ae.BLOCK)
        self.assertEqual(self.ask('Would you like to receive alerts for similar jobs?', ['Yes', 'No'], required=False).answer, 'No')
        kb.put_fact(self.db, 'consent.truthful_certification', 'granted', source='candidate', verification=kb.USER_CONFIRMED)
        self.assertEqual(self.ask('I certify that the information I provide is true and complete', kind='checkbox').answer, 'Yes')
        self.assertEqual(self.ask('I certify the information is true and complete and I hereby waive all rights to bring any action for defamation',
                                  kind='checkbox').action, ae.BLOCK)
        self.assertEqual(self.ask('I agree to the privacy policy and to binding arbitration', kind='checkbox').action, ae.BLOCK)

    def test_demographics_decline_or_omit(self):
        self.assertEqual(self.ask('Gender', ['Male', 'Female', 'Decline to self-identify']).answer, 'Decline to self-identify')
        self.assertEqual(self.ask('Gender', ['Male', 'Female'], required=False).action, ae.OMIT)
        self.assertEqual(self.ask('Gender', ['Male', 'Female'], required=True).action, ae.BLOCK)

    def test_compensation_units(self):
        self.assertEqual(self.ask('Current CTC (in lacs)').answer, '8')
        self.assertEqual(self.ask('Expected annual salary in INR', kind='number').answer, '1500000')
        self.assertEqual(self.ask('Expected monthly salary (INR)').answer, '125000')
        self.assertEqual(self.ask('Expected salary in USD').action, ae.BLOCK)
        us = ae.Context(job_id=5, job_country='US', today=TODAY)
        self.assertEqual(self.ask('Expected compensation', kind='number', ctx=us).action, ae.BLOCK)

    def test_free_text_only_from_covering_cards(self):
        result = self.ask('Describe a challenging problem you solved', kind='textarea')
        self.assertEqual(result.evidence, ['card:latency'])
        self.assertEqual(self.ask('Describe your experience with Kubernetes', kind='textarea').action, ae.BLOCK)
        self.assertEqual(self.ask('Why do you want to join Okta?', kind='textarea').action, ae.BLOCK)

    def test_conflicting_live_values_answer_nothing_until_confirmed(self):
        kb.put_fact(self.db, 'notice_period_days', 60, source='config/other.json', verification=kb.USER_SAVED)
        self.assertIsNone(kb.get_fact(self.db, 'notice_period_days'))
        self.assertEqual(self.ask('Notice period (days)', kind='number').action, ae.BLOCK)
        kb.put_fact(self.db, 'notice_period_days', 45, source='candidate', verification=kb.USER_CONFIRMED)
        self.assertEqual(self.ask('Notice period (days)', kind='number').answer, '45')
        self.assertEqual(len(kb.fact_history(self.db, 'notice_period_days')), 3)

    def test_option_labels_and_location_questions(self):
        self.assertEqual(self.ask('Less than 2 years — I have limited professional experience developing mobile applications').action, ae.BLOCK)
        self.assertEqual(self.ask('Are you currently in Pune?', ['Yes', 'No']).answer, 'No')
        self.assertEqual(self.ask('Are you comfortable working from our Bengaluru office?', ['Yes', 'No']).answer, 'Yes')
        self.assertIsNone(ae.pick_text(['Yes', 'No'], 'Test University of Technology'))

    def test_learned_wording_maps_intent_without_saving_an_answer(self):
        question = 'Kindly mention your joining availability'
        kb.learn_template(self.db, question, 'notice_period')
        self.assertEqual(self.ask(question).answer, '30 days')
        self.assertIsNone(kb.get_fact(self.db, 'answer.question:' + ae.norm(question)))

    def test_import_is_idempotent_and_skips_secrets(self):
        before = self.db.execute('SELECT COUNT(*) FROM facts').fetchone()[0]
        counts = kb.import_legacy(self.db, PROFILE, dict(ANSWERS, default_password='x'), SEED)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM facts').fetchone()[0], before)
        self.assertEqual(counts['skipped_secret'], 1)


class ReplayGateTests(unittest.TestCase):
    def test_replay_auditor_flags_misrouted_answers(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('replay_answers', ROOT / 'scripts' / 'replay_answers.py')
        replay = importlib.util.module_from_spec(spec); spec.loader.exec_module(replay)
        self.assertEqual(replay.category('Will you require visa sponsorship?'), 'sponsorship')
        self.assertEqual(replay.category('How many years of experience with Python?'), 'skill_years')
        self.assertEqual(replay.category('I acknowledge the privacy notice'), 'consent')


if __name__ == '__main__':
    unittest.main()
