import unittest
from unittest.mock import Mock, patch

import jobpilot
from ats_forms import supports_autosubmit
from matching.requirements import evaluate_experience_requirements


class AutonomousPlanTests(unittest.TestCase):
    def test_four_year_posting_does_not_inflate_candidate_experience(self):
        rules = jobpilot.load_json('eligibility.json')
        profile = jobpilot.load_json('profile.json')
        self.assertEqual(rules['maxExperienceYears'], 4)
        self.assertEqual(profile['experience']['years'], 3)
        self.assertTrue(evaluate_experience_requirements('0-4 years of experience', 3, 4).is_eligible)
        self.assertFalse(evaluate_experience_requirements('4 years of software development experience', 3, 4).is_eligible)

    def test_naukri_requires_validation_before_unattended_submission(self):
        self.assertTrue(supports_autosubmit('naukri'))
        self.assertFalse(supports_autosubmit('indeed'))

    def test_recruiter_answers_use_saved_facts_and_units(self):
        self.assertEqual(jobpilot.question_answer('What is your total experience?')[0], '3')
        self.assertEqual(jobpilot.question_answer('What is your notice period in months?')[0], '1')
        self.assertEqual(jobpilot.question_answer('What is your current CTC in lakhs?')[0], '8')
        self.assertIsNone(jobpilot.question_answer('What is your expected CTC in USD?')[0])

    def test_naukri_detail_keeps_relative_posting_age_out_of_exact_timestamp(self):
        page = Mock()
        def locator(selector):
            node = Mock()
            node.first.count.return_value = 1
            values = {
                '.styles_JDC__dang-inner-html__h0K4t':'Build .NET services with 2 years of experience',
                '[class*="dang-inner-html"]':'', '[class*="job-desc"]':'',
                'body':'Developer | 2 - 5 years | Hyderabad',
                '[class*="posted"]':'Posted 7 days ago'}
            node.first.inner_text.return_value = values.get(selector, '')
            node.inner_text.return_value = values.get(selector, '')
            node.all_text_contents.return_value = []
            return node
        page.locator.side_effect = locator
        with patch('jobpilot.visible_page_text', return_value='Apply'), \
             patch('jobpilot.naukri_direct_apply_control', return_value=object()):
            facts = jobpilot.naukri_detail(page)
        self.assertEqual((facts['experience_min'], facts['experience_max']), (2, 5))
        self.assertEqual(facts['apply_type'], 'direct')
        self.assertEqual(facts['posted_at'], '')
        self.assertEqual(facts['posted_raw'], 'Posted 7 days ago')


if __name__ == '__main__':
    unittest.main()
