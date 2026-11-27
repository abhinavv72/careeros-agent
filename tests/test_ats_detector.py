import unittest

from ats_detector import detect_url


class AtsDetectorTests(unittest.TestCase):
    def test_detects_supported_ats_from_employer_url(self):
        cases = {
            'https://boards.greenhouse.io/acme/jobs/1': 'greenhouse',
            'https://jobs.lever.co/acme/abc/apply': 'lever',
            'https://acme.wd5.myworkdayjobs.com/en-US/jobs/1': 'workday',
            'https://jobs.smartrecruiters.com/acme/1': 'smartrecruiters',
            'https://jobs.ashbyhq.com/acme/1': 'ashby',
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_url(url)[0], expected)

    def test_unknown_page_remains_generic(self):
        self.assertEqual(detect_url('https://example.com/apply/1')[0], 'generic')


if __name__ == '__main__':
    unittest.main()
