import unittest
import profile_agent


class ProfileAgentTests(unittest.TestCase):
    def test_employee_relationship_answer_is_reused(self):
        self.assertEqual(profile_agent.answer('Do you have a personal or professional relationship with any current employee?',{}, {'employee_relationship':'No'}),'No')
    def test_salary_converts_lpa_to_annual_inr(self):
        self.assertEqual(profile_agent.answer('Desired salary in INR',{}, {'expected_compensation':'15 LPA'}), '1500000')

    def test_compensation_expectation_uses_saved_expected_compensation(self):
        self.assertEqual(profile_agent.answer('Compensation Expectation? (Total Annual Comp)',{}, {'expected_compensation':'15 LPA'}), '15 LPA')

    def test_salary_does_not_convert_inr_to_an_unverified_usd_amount(self):
        self.assertIsNone(profile_agent.answer('What is your expected annual salary in USD?',{}, {'expected_compensation':'15 LPA'}))
        self.assertEqual(profile_agent.answer('What is your expected annual salary?',{}, {'expected_compensation':'15 LPA'}),'15 LPA')

    def test_unknown_facts_are_not_inferred(self):
        self.assertIsNone(profile_agent.answer('State you reside in', {'personal':{'city':'Hyderabad'}}, {}))

    def test_city_uses_saved_profile(self):
        self.assertEqual(profile_agent.answer('City you reside in', {'personal':{'city':'Hyderabad'}}, {}), 'Hyderabad')

    def test_sensitive_demographic_answers_require_explicit_user_action(self):
        self.assertIsNone(profile_agent.answer('What is your gender?', {'personal':{}}, {}))
        self.assertIsNone(profile_agent.answer('What is your sexual orientation?', {'personal':{}}, {}))

    def test_work_authorization_is_only_reused_for_a_saved_country(self):
        self.assertEqual(profile_agent.answer('Are you authorized to work in India?', {}, {'work_authorization_india':'Yes'}), 'Yes')
        self.assertIsNone(profile_agent.answer('Are you authorized to work in the United States?', {}, {'work_authorization_india':'Yes'}))
