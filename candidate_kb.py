"""Versioned candidate knowledge base (``data/candidate.db``).

Facts are append-only: every write adds a new version of ``(key, scope)``. Each row
records where the value came from and how it was verified. Only rows whose
``verification`` is in :data:`LIVE` can feed an application; legacy guesses are kept
for review but never answer a question. Two live values for the same key and scope
that disagree are a *conflict*, and a conflicted fact answers nothing until resolved.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / 'data' / 'candidate.db'
IMPORT_VERSION = '2'  # bump when import_legacy's mapping changes so databases re-import

USER_CONFIRMED = 'user_confirmed'   # the candidate confirmed this exact value
USER_SAVED = 'user_saved'           # a structured value the candidate saved in config
RESUME = 'resume'                   # stated in the candidate's own résumé
DERIVED = 'derived'                 # computed from other live facts
LEGACY = 'legacy_unverified'        # imported guess; never used live
LIVE = (USER_CONFIRMED, USER_SAVED, RESUME, DERIVED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'global',
  value TEXT NOT NULL, source TEXT NOT NULL, verification TEXT NOT NULL,
  version INTEGER NOT NULL, updated_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  UNIQUE(key, scope, version));
CREATE INDEX IF NOT EXISTS ix_facts_key ON facts(key, scope, version DESC);
CREATE TABLE IF NOT EXISTS employment(
  id TEXT PRIMARY KEY, employer TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', title TEXT NOT NULL,
  location TEXT, type TEXT NOT NULL, start TEXT NOT NULL, end TEXT,
  source TEXT NOT NULL, verification TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS education(
  id TEXT PRIMARY KEY, institution TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', degree TEXT,
  grade TEXT, start TEXT, end TEXT, location TEXT, source TEXT NOT NULL, verification TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skill_usage(
  id INTEGER PRIMARY KEY, skill TEXT NOT NULL, context TEXT NOT NULL, employment_id TEXT,
  start TEXT NOT NULL, end TEXT, source TEXT NOT NULL, verification TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(skill, context, employment_id, start));
CREATE TABLE IF NOT EXISTS evidence_cards(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, employment_id TEXT, claim TEXT NOT NULL,
  contribution TEXT NOT NULL DEFAULT '', skills TEXT NOT NULL DEFAULT '[]', tags TEXT NOT NULL DEFAULT '[]',
  evidence_link TEXT, note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, verification TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS question_templates(
  normalized TEXT PRIMARY KEY, intent TEXT NOT NULL, param TEXT, learned_at TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS answer_blockers(
  id INTEGER PRIMARY KEY, job_id INTEGER, question TEXT NOT NULL, intent TEXT, reason TEXT NOT NULL,
  needs TEXT, created_at TEXT NOT NULL, resolved_at TEXT, UNIQUE(job_id, question));
"""


def now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def norm(text: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9+#. ]+', ' ', (text or '').casefold())).strip(' .')


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# ------------------------------------------------------------------ facts

@dataclass(frozen=True)
class Fact:
    key: str
    scope: str
    value: object
    source: str
    verification: str
    version: int
    updated_at: str

    @property
    def ref(self) -> str:
        return f'fact:{self.key}@{self.scope}#v{self.version}'


def _row_fact(row) -> Fact:
    return Fact(row['key'], row['scope'], json.loads(row['value']), row['source'], row['verification'],
                row['version'], row['updated_at'])


def put_fact(db, key: str, value, *, source: str, verification: str, scope: str = 'global', note: str = '', commit: bool = True) -> Fact:
    """Append a new version unless the latest version from this source is identical."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    latest = db.execute('SELECT * FROM facts WHERE key=? AND scope=? ORDER BY version DESC LIMIT 1', (key, scope)).fetchone()
    if latest and latest['value'] == encoded and latest['source'] == source and latest['verification'] == verification:
        return _row_fact(latest)
    version = (latest['version'] + 1) if latest else 1
    db.execute('INSERT INTO facts(key,scope,value,source,verification,version,updated_at,note) VALUES(?,?,?,?,?,?,?,?)',
               (key, scope, encoded, source, verification, version, now(), note))
    if commit:
        db.commit()
    return _row_fact(db.execute('SELECT * FROM facts WHERE key=? AND scope=? AND version=?', (key, scope, version)).fetchone())


def fact_history(db, key: str, scope: str = 'global') -> list[Fact]:
    return [_row_fact(r) for r in db.execute('SELECT * FROM facts WHERE key=? AND scope=? ORDER BY version', (key, scope))]


def _current_by_source(db, key, scope):
    """Latest live version per source; a user confirmation supersedes everything older."""
    rows = db.execute('SELECT * FROM facts WHERE key=? AND scope=? ORDER BY version', (key, scope)).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        if row['verification'] == USER_CONFIRMED:
            latest = {}  # an explicit confirmation resolves earlier disagreement
        latest[row['source']] = row
    return [r for r in latest.values() if r['verification'] in LIVE]


def get_fact(db, key: str, scope: str = 'global') -> Fact | None:
    """The live value, or None when unknown or conflicted."""
    current = _current_by_source(db, key, scope)
    if not current:
        return None
    values = {r['value'] for r in current}
    if len(values) > 1:
        return None
    return _row_fact(max(current, key=lambda r: r['version']))


def conflicts(db) -> list[dict]:
    out = []
    for row in db.execute('SELECT DISTINCT key, scope FROM facts').fetchall():
        current = _current_by_source(db, row['key'], row['scope'])
        if len({r['value'] for r in current}) > 1:
            out.append({'key': row['key'], 'scope': row['scope'],
                        'values': [{'value': json.loads(r['value']), 'source': r['source'], 'verification': r['verification']}
                                   for r in current]})
    return out


def legacy_facts(db) -> list[Fact]:
    rows = db.execute("""SELECT f.* FROM facts f WHERE verification=? AND version=(SELECT MAX(version) FROM facts g
        WHERE g.key=f.key AND g.scope=f.scope) ORDER BY key""", (LEGACY,)).fetchall()
    return [_row_fact(r) for r in rows]


# ------------------------------------------------------------------ dated records

def _month_start(value: str) -> date:
    year, month = map(int, value.split('-')[:2])
    return date(year, month, 1)


def _month_end(value: str | None, as_of: date) -> date:
    if not value:
        return as_of
    year, month = map(int, value.split('-')[:2])
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return min(as_of, date.fromordinal(nxt.toordinal() - 1))


def merged_years(intervals, as_of: date) -> float:
    """Total years covered by ``(start, end)`` month strings, overlaps counted once."""
    spans = sorted((_month_start(s), _month_end(e, as_of)) for s, e in intervals if _month_start(s) <= as_of)
    total, cur_start, cur_end = 0, None, None
    for start, end in spans:
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += (cur_end - cur_start).days + 1
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        total += (cur_end - cur_start).days + 1
    return total / 365.25


def employment(db, live_only: bool = True) -> list[dict]:
    rows = db.execute('SELECT * FROM employment ORDER BY start DESC').fetchall()
    return [dict(r, aliases=json.loads(r['aliases'])) for r in rows if not live_only or r['verification'] in LIVE]


def education(db) -> list[dict]:
    return [dict(r, aliases=json.loads(r['aliases'])) for r in db.execute('SELECT * FROM education') if r['verification'] in LIVE]


def total_experience(db, as_of: date | None = None, include_internships: bool = False) -> tuple[float, list[str]]:
    as_of = as_of or date.today()
    jobs = [e for e in employment(db) if include_internships or e['type'] == 'full_time']
    return merged_years([(e['start'], e['end']) for e in jobs], as_of), [f"employment:{e['id']}" for e in jobs]


SKILL_ALIASES = {
    'c#': {'c#', 'csharp', 'c sharp'}, '.net': {'.net', 'dotnet', 'dot net', 'asp.net', 'asp.net core', '.net core', '.net 8', '.net framework'},
    'python': {'python'}, 'java': {'java'}, 'javascript': {'javascript', 'js'}, 'typescript': {'typescript', 'ts'},
    'sql': {'sql', 't-sql', 'tsql'}, 'react': {'react', 'react.js', 'reactjs'}, 'angular': {'angular'},
    'go': {'go', 'golang'}, 'ruby': {'ruby'}, 'kubernetes': {'kubernetes', 'k8s'}, 'docker': {'docker'},
    'aws': {'aws', 'amazon web services'}, 'gcp': {'gcp', 'google cloud'}, 'azure': {'azure'},
    'c++': {'c++', 'cpp'}, 'node.js': {'node', 'node.js', 'nodejs'}, 'postgresql': {'postgresql', 'postgres'},
    'mysql': {'mysql'}, 'mongodb': {'mongodb', 'mongo'}, 'spring boot': {'spring boot', 'spring'},
}


def canonical_skill(name: str) -> str:
    n = norm(name)
    for canon, names in SKILL_ALIASES.items():
        if n in names:
            return canon
    return n


def skill_experience(db, skills, as_of: date | None = None, contexts=('professional',)) -> tuple[float, list[str]] | None:
    """Years of confirmed use of any of ``skills``; None when no confirmed usage exists."""
    as_of = as_of or date.today()
    wanted = {canonical_skill(s) for s in skills}
    rows = [r for r in db.execute('SELECT * FROM skill_usage').fetchall()
            if r['verification'] in (USER_CONFIRMED, USER_SAVED) and canonical_skill(r['skill']) in wanted
            and r['context'] in contexts]
    if not rows:
        return None
    return merged_years([(r['start'], r['end']) for r in rows], as_of), [f"skill_usage:{r['id']}" for r in rows]


def confirm_skill(db, skill: str, start: str, end: str | None = None, *, employment_id: str | None = None,
                  context: str = 'professional') -> None:
    db.execute("""INSERT INTO skill_usage(skill,context,employment_id,start,end,source,verification,updated_at)
        VALUES(?,?,?,?,?,'cli',?,?) ON CONFLICT(skill,context,employment_id,start) DO UPDATE SET end=excluded.end,
        verification=excluded.verification,updated_at=excluded.updated_at""",
               (canonical_skill(skill), context, employment_id, start, end, USER_CONFIRMED, now()))
    db.commit()


def cards(db) -> list[dict]:
    return [dict(r, skills=json.loads(r['skills']), tags=json.loads(r['tags']))
            for r in db.execute('SELECT * FROM evidence_cards') if r['verification'] in LIVE]


def claimed_skills(db) -> set[str]:
    fact = get_fact(db, 'skills.claimed')
    names = set(fact.value) if fact else set()
    for card in cards(db):
        names.update(card['skills'])
    return {canonical_skill(n) for n in names}


# ------------------------------------------------------------------ templates & blockers

def learn_template(db, question: str, intent: str, param: str | None = None, source: str = 'cli') -> None:
    db.execute('INSERT OR REPLACE INTO question_templates(normalized,intent,param,learned_at,source) VALUES(?,?,?,?,?)',
               (norm(question), intent, param, now(), source))
    db.commit()


def template_for(db, question: str):
    return db.execute('SELECT intent,param FROM question_templates WHERE normalized=?', (norm(question),)).fetchone()


def record_blocker(db, job_id, question, intent, reason, needs=None) -> None:
    db.execute("""INSERT INTO answer_blockers(job_id,question,intent,reason,needs,created_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT(job_id,question) DO UPDATE SET intent=excluded.intent,reason=excluded.reason,needs=excluded.needs,resolved_at=NULL""",
               (job_id, question[:500], intent, reason, needs, now()))
    db.commit()


# ------------------------------------------------------------------ import

# Structured keys in answers.json the candidate wrote deliberately. Anything else in
# that file (question-text keys, ``exact_questions``) was written by the old resolver.
SAVED_KEYS = {
    'work_authorization_india': ('work_authorization', 'country:IN'),
    'work_authorization_us': ('work_authorization', 'country:US'),
    'work_authorization_uk': ('work_authorization', 'country:GB'),
    'work_authorization_canada': ('work_authorization', 'country:CA'),
    'willing_to_relocate': ('preference.willing_to_relocate', 'global'),
    'notice_period': ('notice_period', 'global'),
    'current_compensation': ('compensation.current', 'currency:INR'),
    'expected_compensation': ('compensation.expected', 'currency:INR'),
    'employee_relationship': ('relationship_with_employee', 'global'),
    'age_18_or_older': ('age_18_or_older', 'global'),
    'product_company_experience': ('answer.product_company_experience', 'global'),
    'transaction_systems_experience': ('answer.transaction_systems_experience', 'global'),
    'primary_programming_languages_frameworks': ('answer.primary_languages', 'global'),
    'why_good_fit': ('answer.career_goals', 'global'),
    'outside_business_activity': ('outside_business_activity', 'global'),
    'previous_employer_relationship': ('previous_employer_relationship', 'global'),
    'application_source': ('preference.application_source', 'global'),
    'sms_communications_consent': ('consent.sms', 'global'),
}
# Structured keys that assign one duration or affirmation to every skill.
SHARED_GUESS_KEYS = ('default_tool_experience_years', 'assume_technical_skill_experience', 'tool_experience_years',
                     'fullstack_years', 'years_experience')
SECRET_KEY = re.compile(r'password|secret|token|otp', re.I)


def import_legacy(db, profile: dict, answers: dict, seed: dict | None = None, as_of: date | None = None) -> dict:
    """Idempotently import config files. Returns counts per verification state."""
    counts = {USER_SAVED: 0, RESUME: 0, LEGACY: 0, 'skipped_secret': 0}
    def _put(key: str, value, *, source: str, verification: str, scope: str = 'global', note: str = ''):
        return put_fact(db, key, value, source=source, verification=verification, scope=scope, note=note, commit=False)
    p, e = profile.get('personal', {}), profile.get('experience', {})
    for key in ('firstName', 'lastName', 'email', 'phone', 'city', 'state', 'country', 'postalCode', 'linkedin', 'github', 'portfolio'):
        if str(p.get(key) or '').strip():
            _put(f'personal.{key}', str(p[key]).strip(), source='config/profile.json', verification=USER_SAVED); counts[USER_SAVED] += 1
    if e.get('noticePeriodDays') is not None:
        _put('notice_period_days', int(e['noticePeriodDays']), source='config/profile.json', verification=USER_SAVED); counts[USER_SAVED] += 1
    if profile.get('preferences', {}).get('locations'):
        _put('preference.locations', list(profile['preferences']['locations']), source='config/profile.json', verification=USER_SAVED)
    if e.get('years') is not None:
        # Kept for comparison only: total experience is derived from dated employment.
        _put('legacy.experience_years', e['years'], source='config/profile.json', verification=LEGACY,
             note='Undated total; superseded by employment records'); counts[LEGACY] += 1

    for key, value in answers.items():
        if key == 'exact_questions' or value is None or value == '':
            continue
        if SECRET_KEY.search(key):
            counts['skipped_secret'] += 1
            continue
        if key == 'notice_period' and re.fullmatch(r'\s*(\d+)\s*days?\s*', str(value), re.I):
            # Same fact as profile.json's noticePeriodDays: disagreement becomes a conflict.
            _put('notice_period_days', int(re.match(r'\s*(\d+)', str(value)).group(1)),
                 source=f'config/answers.json:{key}', verification=USER_SAVED)
            counts[USER_SAVED] += 1
            continue
        if key in SAVED_KEYS:
            fact_key, scope = SAVED_KEYS[key]
            _put(fact_key, value, scope=scope, source=f'config/answers.json:{key}', verification=USER_SAVED)
            counts[USER_SAVED] += 1
        elif key in SHARED_GUESS_KEYS:
            _put(f'legacy.{key}', value, source=f'config/answers.json:{key}', verification=LEGACY,
                 note='One value applied to many skills; confirm per skill instead'); counts[LEGACY] += 1
        else:
            _put(f'legacy.answer.{norm(key)[:200]}', value, source='config/answers.json', verification=LEGACY); counts[LEGACY] += 1
    for question, value in (answers.get('exact_questions') or {}).items():
        if not question or SECRET_KEY.search(question) or value in (None, ''):
            continue
        _put(f'legacy.answer.{norm(question)[:200]}', value, source='config/answers.json:exact_questions',
             verification=LEGACY, note='Saved by the old resolver; review before use'); counts[LEGACY] += 1
    # The candidate saved "No" without a country. It is only certainly true where they
    # are authorized to work, so scope it there; other countries stay unknown.
    if answers.get('require_sponsorship') not in (None, ''):
        for scope in ('country:IN',):
            if str(answers.get('work_authorization_india', '')).casefold() == 'yes':
                _put('require_sponsorship', answers['require_sponsorship'], scope=scope,
                     source='config/answers.json:require_sponsorship', verification=USER_SAVED,
                     note='Saved without a country; scoped to India where work authorization is saved')
                counts[USER_SAVED] += 1

    if seed:
        src = seed.get('source', 'config/candidate-seed.json')
        for job in seed.get('employment', []):
            db.execute("""INSERT INTO employment(id,employer,aliases,title,location,type,start,end,source,verification,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET employer=excluded.employer,aliases=excluded.aliases,
                title=excluded.title,location=excluded.location,type=excluded.type,start=excluded.start,end=excluded.end,
                source=excluded.source,updated_at=excluded.updated_at
                WHERE employment.verification<>'user_confirmed'""",
                       (job['id'], job['employer'], json.dumps(job.get('aliases', [])), job['title'], job.get('location'),
                        job['type'], job['start'], job.get('end'), src, RESUME, now()))
            counts[RESUME] += 1
        for edu in seed.get('education', []):
            db.execute("""INSERT OR REPLACE INTO education(id,institution,aliases,degree,grade,start,end,location,source,verification)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (edu['id'], edu['institution'], json.dumps(edu.get('aliases', [])), edu.get('degree'),
                                                 edu.get('grade'), edu.get('start'), edu.get('end'), edu.get('location'), src, RESUME))
        if seed.get('employment_history_complete') is not None:
            _put('employment.history_complete', bool(seed['employment_history_complete']), source=src, verification=RESUME)
        if seed.get('skills_claimed'):
            _put('skills.claimed', sorted(seed['skills_claimed']), source=src, verification=RESUME)
        for card in seed.get('evidence_cards', []):
            db.execute("""INSERT OR REPLACE INTO evidence_cards(id,kind,employment_id,claim,contribution,skills,tags,evidence_link,note,source,verification,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (card['id'], card['kind'], card.get('employment_id'), card['claim'],
                                                     card.get('contribution', ''), json.dumps(card.get('skills', [])),
                                                     json.dumps(card.get('tags', [])), card.get('evidence_link'), card.get('note', ''), src, RESUME, now()))
    db.commit()
    return counts
