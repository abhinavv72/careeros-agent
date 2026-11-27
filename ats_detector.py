"""URL and DOM based ATS detection inspired by the vendored job-apply-ai design."""
from urllib.parse import urlparse

URL_PATTERNS = {
    'workday': ('myworkdayjobs.com', 'workday.com/job'),
    'greenhouse': ('greenhouse.io',),
    'lever': ('lever.co',),
    'icims': ('icims.com',),
    'smartrecruiters': ('smartrecruiters.com',),
    'ashby': ('ashbyhq.com',),
    'taleo': ('taleo.net',),
    'oracle': ('oraclecloud.com',),
    'teamtailor': ('teamtailor.com',),
    'jobvite': ('jobvite.com',),
    'workable': ('workable.com',),
}


def detect_url(url: str) -> tuple[str, float]:
    value = (url or '').casefold()
    for ats, patterns in URL_PATTERNS.items():
        if any(pattern in value for pattern in patterns):
            return ats, 0.95
    return 'generic', 0.0


def detect_page(page, url: str = '') -> tuple[str, float]:
    ats, confidence = detect_url(url or getattr(page, 'url', ''))
    if ats != 'generic':
        return ats, confidence
    signatures = {
        'greenhouse': ('#application_form', 'text=Powered by Greenhouse'),
        'lever': ('.lever-application-form', 'text=Powered by Lever'),
        'workday': ('[data-automation-id="workday"]', 'text=Powered by Workday'),
        'icims': ('.iCIMS_MainWrapper',),
        'smartrecruiters': ('[data-test="header-smartrecruiters"]', 'text=Powered by SmartRecruiters'),
        'ashby': ('[data-testid="ashby-job-posting"]', 'text=Powered by Ashby'),
    }
    for name, selectors in signatures.items():
        try:
            if any(page.locator(selector).count() for selector in selectors):
                return name, 0.8
        except Exception:
            continue
    return 'generic', 0.0
