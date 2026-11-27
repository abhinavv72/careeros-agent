import unittest
import importlib
import socket
from unittest.mock import patch

import jobpilot


class FakeLocator:
    def __init__(self, fields=()):
        self.fields = list(fields)

    def count(self):
        return len(self.fields)

    def all(self):
        return self.fields


class FakeContext:
    def __init__(self, locators=None):
        self.locators = locators or {}

    def locator(self, selector):
        return self.locators.get(selector, FakeLocator())

    def get_by_text(self, _pattern):
        return self.locators.get('confirmation', FakeLocator())


class EmailVerificationTests(unittest.TestCase):
    def test_importing_email_verifier_does_not_change_process_socket_timeout(self):
        original = socket.getdefaulttimeout()
        with patch('socket.setdefaulttimeout') as set_default_timeout:
            import gmail_verifier
            importlib.reload(gmail_verifier)
            set_default_timeout.assert_not_called()
        self.assertEqual(socket.getdefaulttimeout(), original)

    def test_detects_standard_one_time_code_input(self):
        code_inputs = FakeLocator([object()] * 8)
        page = FakeContext({'input[autocomplete="one-time-code"]:visible': code_inputs})
        with patch('jobpilot.visible_page_text', return_value='Enter the verification code sent to your email'):
            context, inputs = jobpilot.email_verification_inputs(page, page)
        self.assertIs(context, page)
        self.assertIs(inputs, code_inputs)

    def test_searches_employer_iframes_for_verification_inputs(self):
        frame = FakeContext({'input[id^="security-input-"]:visible': FakeLocator([object()] * 6)})
        page = FakeContext()
        page.frames = [frame]

        def visible_text(context):
            return 'A verification code is required' if context is frame else ''

        with patch('jobpilot.visible_page_text', side_effect=visible_text):
            context, inputs = jobpilot.email_verification_inputs(page, page)
        self.assertIs(context, frame)
        self.assertEqual(inputs.count(), 6)

    def test_does_not_treat_unrelated_security_input_as_an_otp(self):
        page = FakeContext({'input[autocomplete="one-time-code"]:visible': FakeLocator([object()])})
        with patch('jobpilot.visible_page_text', return_value='Application form'):
            context, inputs = jobpilot.email_verification_inputs(page, page)
        self.assertIsNone(context)
        self.assertIsNone(inputs)

    def test_finds_confirmation_inside_an_employer_frame(self):
        class Confirmation:
            def is_visible(self): return True
        frame=FakeContext({'confirmation':FakeLocator([Confirmation()])})
        page=FakeContext()
        page.frames=[frame]
        self.assertIs(jobpilot.visible_submission_confirmation(page,page,'Thank you'),
                      frame.locators['confirmation'].fields[0])


class VerificationCodeExtractionTests(unittest.TestCase):
    def setUp(self):
        import gmail_verifier
        self.extract = gmail_verifier.extract_verification_code

    def test_greenhouse_security_code_email(self):
        text = ('Security code for your application to Acme Copy and paste this code into the security code '
                'field on your application: Ab3dE7Gh After you enter the code, resubmit your application.')
        self.assertEqual(self.extract(text, 8), ('Ab3dE7Gh', 'contextual'))

    def test_prefix_of_a_longer_number_is_never_a_code(self):
        text = 'Your verification code: 1234567890 is your reference number.'
        self.assertEqual(self.extract(text, 8), (None, None))

    def test_ignores_ordinary_eight_letter_words(self):
        self.assertEqual(self.extract('Please resubmit your Software application to Greenhouse', 8), (None, None))


if __name__ == '__main__':
    unittest.main()
