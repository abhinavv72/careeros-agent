"""Import India-eligible remote leads from Remotive's public API."""
from dataclasses import asdict
from urllib.parse import urlparse

import jobpilot

API = 'https://remotive.com/api/remote-jobs?limit=200'


def india_eligible(location: str) -> bool:
    value = jobpilot.norm(location)
    return any(token in value for token in ('india', 'worldwide', 'anywhere', 'global'))


def target_role(title: str) -> bool:
    value = jobpilot.norm(title)
    return any(token in value for token in (
        'software', 'backend', 'back end', 'full stack', 'fullstack', '.net',
        'developer', 'devops', 'platform engineer', 'cloud engineer',
        'security engineer', 'site reliability', 'sre'))


def discover(limit: int = 200) -> dict:
    # Remotive recommends no more than four requests per day. The scheduler
    # calls this source every six hours and the API returns newest first.
    data = jobpilot.fetch_json(API)
    imported = duplicates = excluded = 0
    db = jobpilot.connect()
    try:
        for item in data.get('jobs', [])[:limit]:
            title = str(item.get('title') or '').strip()
            location = str(item.get('candidate_required_location') or '').strip()
            url = str(item.get('url') or '')
            parsed = urlparse(url)
            if not title or not target_role(title) or not india_eligible(location) or parsed.scheme != 'https' or parsed.hostname != 'remotive.com':
                excluded += 1
                continue
            job = jobpilot.Job(str(item.get('id') or url), str(item.get('company_name') or 'Unknown'),
                title, jobpilot.plain_html(str(item.get('description') or '')),
                f'Remote {location}', 'Remotive', url, url, 'remotive',
                str(item.get('publication_date') or ''))
            before = db.total_changes
            db.execute("""INSERT OR IGNORE INTO jobs(external_id,company,title,description,location,source,source_url,apply_url,ats,posted_at,discovered_at,fingerprint,description_hash)
                VALUES(:external_id,:company,:title,:description,:location,:source,:source_url,:apply_url,:ats,:posted_at,:discovered_at,:fingerprint,:description_hash)""",
                asdict(job) | {'discovered_at':jobpilot.now(),'fingerprint':job.fingerprint,'description_hash':job.description_hash})
            if db.total_changes > before:
                imported += 1
            else:
                duplicates += 1
        db.commit()
    finally:
        db.close()
    print(f'Remotive: {imported} new India-eligible remote leads, {duplicates} known, {excluded} excluded', flush=True)
    return {'imported':imported,'duplicates':duplicates,'excluded':excluded}
