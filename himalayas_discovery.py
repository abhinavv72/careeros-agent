"""Import India-eligible remote leads from Himalayas' public search API."""
from dataclasses import asdict
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

import jobpilot
from remote_discovery import target_role

TERMS = ('backend', '.NET', 'full stack', 'platform engineer', 'cloud engineer', 'security engineer')


def discover(term: str | None = None) -> dict:
    if term is None:
        slot = int(datetime.now(timezone.utc).timestamp() // 21600)
        term = TERMS[slot % len(TERMS)]
    url = 'https://himalayas.app/jobs/api/search?' + urlencode({'country':'IN','q':term,'sort':'recent','page':1})
    data = jobpilot.fetch_json(url)
    imported = duplicates = excluded = 0
    db = jobpilot.connect()
    try:
        for item in data.get('jobs', []):
            title = str(item.get('title') or '').strip()
            link = str(item.get('applicationLink') or '')
            parsed = urlparse(link)
            locations = item.get('locationRestrictions') or []
            accepted = not locations or any(str(value).casefold() in ('india','in') for value in locations)
            if not title or not target_role(title) or not accepted or parsed.scheme != 'https' or parsed.hostname != 'himalayas.app':
                excluded += 1
                continue
            location = 'Remote India' if locations else 'Remote Worldwide'
            guid = str(item.get('guid') or link)
            posted_raw=item.get('pubDate')
            if isinstance(posted_raw,(int,float)):
                posted_at=datetime.fromtimestamp(posted_raw,timezone.utc).isoformat()
            elif isinstance(posted_raw,str):
                try:
                    posted_date=datetime.fromisoformat(posted_raw.replace('Z','+00:00'))
                    if posted_date.tzinfo is None:
                        posted_date=posted_date.replace(tzinfo=timezone.utc)
                    posted_at=posted_date.astimezone(timezone.utc).isoformat()
                except ValueError:
                    posted_at=''
            else:
                posted_at=''
            job = jobpilot.Job(guid, str(item.get('companyName') or 'Unknown'), title,
                jobpilot.plain_html(str(item.get('description') or '')), location,
                'Himalayas', link, link, 'himalayas', posted_at)
            before = db.total_changes
            db.execute("""INSERT OR IGNORE INTO jobs(external_id,company,title,description,location,source,source_url,apply_url,ats,posted_at,discovered_at,fingerprint,description_hash)
                VALUES(:external_id,:company,:title,:description,:location,:source,:source_url,:apply_url,:ats,:posted_at,:discovered_at,:fingerprint,:description_hash)""",
                asdict(job) | {'discovered_at':jobpilot.now(),'fingerprint':job.fingerprint,'description_hash':job.description_hash})
            inserted = db.total_changes > before
            row = db.execute('SELECT id FROM jobs WHERE apply_url=?', (link,)).fetchone()
            if row:
                updated_text=posted_at
                db.execute('''INSERT INTO job_details(job_id,apply_type,posted_raw,enriched_at)
                    VALUES(?,'platform_handoff',?,?) ON CONFLICT(job_id) DO UPDATE SET
                    apply_type=excluded.apply_type,posted_raw=excluded.posted_raw,enriched_at=excluded.enriched_at''',
                    (row['id'],f'Himalayas last updated {updated_text}' if updated_text else '',jobpilot.now()))
            if inserted:
                imported += 1
            else:
                duplicates += 1
        db.commit()
    finally:
        db.close()
    print(f'Himalayas ({term}): {imported} new India-eligible remote leads, {duplicates} known, {excluded} excluded',flush=True)
    return {'imported':imported,'duplicates':duplicates,'excluded':excluded}
