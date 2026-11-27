"""One authoritative application state, derived from the legacy tracker columns.

The ``jobs.status`` / ``application_queue.status`` / ``application_attempts`` columns
were written by several code paths with overlapping vocabularies (``ManualReview`` vs
``ReviewRequired``, ``SkippedBlocked`` vs ``Skipped``). Rather than trust either column,
every caller asks :func:`application_state`, which also looks at submission attempts,
receipt files on disk and duplicate listings of the same requisition.

Submission safety rests on two calls:

* :func:`assert_can_start` before a browser attempt is opened;
* :func:`claim_submission` immediately before the final employer submit click. It runs
  inside ``BEGIN IMMEDIATE`` so two processes cannot both claim the same requisition, and
  it honours the dashboard pause marker for autonomous runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlparse

READY = 'Ready'
PREPARING = 'Preparing'
SUBMITTING = 'Submitting'
CONFIRMED = 'Confirmed'
RETRY_SCHEDULED = 'Retry scheduled'
BLOCKED = 'Blocked'
OUTCOME_UNCERTAIN = 'Outcome uncertain'
INACTIVE = 'Inactive'  # never entered the application pipeline (discovered, skipped, rejected)
STATES = (READY, PREPARING, SUBMITTING, CONFIRMED, RETRY_SCHEDULED, BLOCKED, OUTCOME_UNCERTAIN, INACTIVE)

# States in which another submission must never be started for this requisition.
SUBMITTED_STATES = (SUBMITTING, CONFIRMED, OUTCOME_UNCERTAIN)
PAUSE_MARKER = 'autonomy-paused'


class SubmissionBlocked(RuntimeError):
    """The job must not be (re)submitted. ``state`` says why."""

    def __init__(self, message: str, state: str = BLOCKED):
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class StateResult:
    state: str
    reason: str
    duplicate_of: tuple[int, ...] = field(default_factory=tuple)
    evidence: str = ''

    def as_dict(self) -> dict:
        return {'state': self.state, 'reason': self.reason, 'duplicate_of': list(self.duplicate_of),
                'evidence': self.evidence}


# ---------------------------------------------------------------- duplicates

_UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
_REQUISITION_PATTERNS = (
    ('greenhouse', re.compile(r'greenhouse\.io/.*?/jobs/(\d+)', re.I)),
    ('greenhouse', re.compile(r'[?&]gh_jid=(\d+)', re.I)),
    ('lever', re.compile(r'lever\.co/[^/]+/(' + _UUID + ')', re.I)),
    ('ashby', re.compile(r'ashbyhq\.com/[^/]+/(' + _UUID + ')', re.I)),
    ('workday', re.compile(r'myworkdayjobs\.com/.*_(R-?\d+(?:-\d+)?)(?:[/?#]|$)', re.I)),
    ('naukri', re.compile(r'naukri\.com/job-listings-.*-(\d{9,})(?:[/?#]|$)', re.I)),
)


def _company_key(company: str | None) -> str:
    return re.sub(r'[^a-z0-9]', '', (company or '').casefold())


def canonical_url(url: str | None) -> str:
    """Employer URL without tracking parameters or the ``/apply`` suffix."""
    if not url:
        return ''
    parsed = urlparse(url.strip())
    if not parsed.netloc:
        return ''
    host = parsed.netloc.casefold().removeprefix('www.')
    if host.endswith('greenhouse.io'):
        host = 'greenhouse.io'
    path = re.sub(r'/(?:apply|application)/?$', '', parsed.path.rstrip('/'), flags=re.I)
    # Career sites often carry the job identity in the query (?gh_jid=, ?jk=), so keep
    # every parameter except known tracking ones.
    params = sorted({(k.casefold(), v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
                     if not _TRACKING_PARAM.match(k)})
    query = ('?' + urlencode(params)) if params else ''
    return f'{host}{path}'.casefold() + query


_TRACKING_PARAM = re.compile(r'^(?:utm_.*|gh_src|source|src|ref|referr?er|lever-(?:source|origin).*|trk.*|'
                             r'fbclid|gclid|mc_[a-z]+|_ga|codes?)$', re.I)


def requisition_id(row) -> str:
    """Stable ``ats:id`` for the posting, or '' when none can be read."""
    keys = row.keys() if hasattr(row, 'keys') else row
    for name in ('apply_url', 'source_url'):
        url = row[name] if name in keys else None
        for ats, pattern in _REQUISITION_PATTERNS:
            match = pattern.search(url or '')
            if match:
                return f'{ats}:{match.group(1).casefold()}'
    return ''


def dedupe_keys(row) -> set[str]:
    """Keys that identify the same opening across cross-posts.

    Primary key is employer + requisition ID; the canonical employer application URL is
    the fallback. Two listings are duplicates when they share any key.
    """
    keys = set()
    req = requisition_id(row)
    if req:
        keys.add(f'req:{_company_key(row["company"])}:{req}')
        # Requisition IDs from these ATSs are globally unique, so a cross-post that
        # spelt the employer differently still collides.
        if req.split(':', 1)[0] in ('greenhouse', 'lever', 'ashby'):
            keys.add(f'req:*:{req}')
    for name in ('apply_url', 'source_url'):
        url = canonical_url(row[name] if name in row.keys() else None)
        if url:
            keys.add(f'url:{url}')
    return keys


# IN (not a correlated EXISTS): application_attempts has no job_id index in older databases.
_SUBMITTED_SQL = """SELECT j.id,j.company,j.apply_url,j.source_url,j.status FROM jobs j
    WHERE j.status='Applied' OR j.id IN (SELECT job_id FROM application_attempts
        WHERE step='submit' OR status='Applied')"""


def submitted_duplicates(db, job) -> tuple[int, ...]:
    """IDs of *other* listings of the same opening that already reached submission."""
    keys = dedupe_keys(job)
    if not keys:
        return ()
    found = [other['id'] for other in db.execute(_SUBMITTED_SQL).fetchall()
             if other['id'] != job['id'] and keys & dedupe_keys(other)]
    return tuple(sorted(found))


def duplicate_groups(rows) -> list[list[int]]:
    """Union-find over dedupe keys; returns groups with more than one listing."""
    parent: dict[int, int] = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: dict[str, int] = {}
    for row in rows:
        parent.setdefault(row['id'], row['id'])
        for key in dedupe_keys(row):
            if key in owner:
                a, b = find(owner[key]), find(row['id'])
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                owner[key] = row['id']
    groups: dict[int, list[int]] = {}
    for job_id in parent:
        groups.setdefault(find(job_id), []).append(job_id)
    return sorted((sorted(g) for g in groups.values() if len(g) > 1), key=lambda g: g[0])


# ---------------------------------------------------------------- evidence

def receipt_path(data_dir: Path, job_id: int, attempt_id: int | None = None) -> Path | None:
    screenshots = Path(data_dir) / 'screenshots'
    if attempt_id is not None:
        candidate = screenshots / f'job-{job_id}' / f'attempt-{attempt_id}' / 'receipt.json'
        if candidate.is_file():
            return candidate
    nested = sorted((screenshots / f'job-{job_id}').glob('attempt-*/receipt.json'))
    if nested:
        return nested[-1]
    flat = screenshots / f'job-{job_id}-receipt.json'
    return flat if flat.is_file() else None


# ---------------------------------------------------------------- state

def _derive(db, job, data_dir: Path, timestamp: str | None, duplicates: tuple[int, ...] | None) -> StateResult:
    job_id = job['id']
    attempts = db.execute('SELECT id,status,step,completed_at FROM application_attempts WHERE job_id=? ORDER BY id',
                          (job_id,)).fetchall()
    applied_attempt = next((a for a in reversed(attempts) if a['status'] == 'Applied'), None)
    receipt = receipt_path(data_dir, job_id, applied_attempt['id'] if applied_attempt else None)

    if applied_attempt and receipt:
        return StateResult(CONFIRMED, 'Employer receipt recorded', evidence=str(receipt))
    if job['status'] == 'Applied' or applied_attempt:
        missing = 'receipt file' if applied_attempt else 'confirmed attempt and receipt'
        return StateResult(OUTCOME_UNCERTAIN, f'Marked Applied but the {missing} is missing; reconcile before any action',
                           evidence=str(receipt or ''))
    open_attempt = next((a for a in attempts if a['completed_at'] is None), None)
    if open_attempt:
        if open_attempt['step'] == 'submit':
            return StateResult(SUBMITTING, 'Submit clicked; waiting for employer confirmation')
        return StateResult(PREPARING, 'Application form is being prepared')
    if any(a['step'] == 'submit' for a in attempts):
        return StateResult(OUTCOME_UNCERTAIN, 'Submission started but no employer receipt was recorded; '
                           'check the portal or confirmation email before retrying')

    if duplicates is None:
        duplicates = submitted_duplicates(db, job)
    if duplicates:
        return StateResult(BLOCKED, 'Another listing of this opening was already submitted: job '
                           + ', '.join(map(str, duplicates)), duplicate_of=duplicates)

    queue = db.execute('SELECT status,next_attempt_at FROM application_queue WHERE job_id=?', (job_id,)).fetchone()
    queue_status = queue['status'] if queue else None
    reason = job['eligibility_reason'] or ''
    if job['status'] == 'FailedTemporary' or queue_status == 'FailedTemporary':
        if queue and queue['next_attempt_at']:
            return StateResult(RETRY_SCHEDULED, f"Retry scheduled for {queue['next_attempt_at']}")
        return StateResult(BLOCKED, reason or 'Previous attempt failed before submission; review before retrying')
    if job['status'] == 'Queued' and queue_status == 'Queued':
        return StateResult(READY, 'Queued for application')
    if job['status'] == 'Queued':
        return StateResult(BLOCKED, f'Stale queue state: tracker says Queued but queue says {queue_status or "missing"}')
    if job['status'] in ('ManualReview', 'SkippedBlocked', 'InProgress', 'Applying') or queue_status in ('ReviewRequired', 'SkippedBlocked'):
        return StateResult(BLOCKED, reason or 'Needs review before application')
    return StateResult(INACTIVE, reason or job['status'])


def application_state(db, job_id: int, data_dir: Path, timestamp: str | None = None,
                      duplicates: tuple[int, ...] | None = None) -> StateResult:
    job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not job:
        return StateResult(INACTIVE, 'Job not found')
    return _derive(db, job, Path(data_dir), timestamp, duplicates)


def states_for(db, job_ids, data_dir: Path) -> dict[int, StateResult]:
    """Batch version used by the dashboard; scans submitted listings once."""
    submitted = db.execute(_SUBMITTED_SQL).fetchall()
    index: dict[str, set[int]] = {}
    for row in submitted:
        for key in dedupe_keys(row):
            index.setdefault(key, set()).add(row['id'])
    results = {}
    for job_id in job_ids:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job:
            continue
        dups = set()
        for key in dedupe_keys(job):
            dups |= index.get(key, set())
        dups.discard(job_id)
        results[job_id] = _derive(db, job, Path(data_dir), None, tuple(sorted(dups)))
    return results


# ---------------------------------------------------------------- guards

def pause_reason(data_dir: Path, autonomous: bool | None = None) -> str | None:
    """Return why the final submit must not happen now, or None."""
    stop = os.environ.get('JOBPILOT_STOP_FILE')
    if stop and Path(stop).exists():
        return 'Stop requested in dashboard'
    if autonomous is None:
        autonomous = os.environ.get('JOBPILOT_AUTONOMOUS') == '1'
    if autonomous and (Path(data_dir) / PAUSE_MARKER).exists():
        return 'Autonomous submissions are paused in the dashboard'
    return None


def assert_can_start(db, job_id: int, data_dir: Path) -> StateResult:
    """Refuse to open an attempt for anything already submitted or in flight."""
    result = application_state(db, job_id, data_dir)
    if result.state in SUBMITTED_STATES or result.state == PREPARING or result.duplicate_of:
        raise SubmissionBlocked(f'Job {job_id} is {result.state}: {result.reason}', result.state)
    return result


def claim_submission(db, job_id: int, attempt_id: int, data_dir: Path, autonomous: bool | None = None) -> None:
    """Atomically move ``attempt_id`` to the submit step, or raise.

    Runs in ``BEGIN IMMEDIATE`` so a second process holding a stale queue row blocks
    until this commit and then sees the submit checkpoint. The checkpoint is committed
    *before* the caller clicks submit: a crash afterwards leaves ``Outcome uncertain``,
    never ``Ready``.
    """
    paused = pause_reason(data_dir, autonomous)
    if paused:
        raise KeyboardInterrupt(paused)
    if db.in_transaction:
        db.commit()
    db.execute('BEGIN IMMEDIATE')
    try:
        attempt = db.execute('SELECT id,step,completed_at FROM application_attempts WHERE id=? AND job_id=?',
                             (attempt_id, job_id)).fetchone()
        if not attempt or attempt['completed_at'] is not None:
            raise SubmissionBlocked(f'Attempt {attempt_id} for job {job_id} is not active')
        other = db.execute("""SELECT id FROM application_attempts WHERE job_id=? AND id<>?
            AND (step='submit' OR status='Applied' OR completed_at IS NULL) LIMIT 1""", (job_id, attempt_id)).fetchone()
        if other:
            raise SubmissionBlocked(f'Job {job_id} already has submission attempt {other["id"]}', OUTCOME_UNCERTAIN)
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if job['status'] == 'Applied':
            raise SubmissionBlocked(f'Job {job_id} is already marked Applied', CONFIRMED)
        duplicates = submitted_duplicates(db, job)
        if duplicates:
            raise SubmissionBlocked(f'Job {job_id} duplicates already-submitted job {", ".join(map(str, duplicates))}')
        db.execute("UPDATE jobs SET status='ManualReview',eligibility_reason='Submission started; confirmation pending' WHERE id=?",
                   (job_id,))
        db.execute("UPDATE application_queue SET status='ReviewRequired' WHERE job_id=?", (job_id,))
        db.execute("UPDATE application_attempts SET step='submit' WHERE id=?", (attempt_id,))
        db.commit()
    except BaseException:
        db.rollback()
        raise
