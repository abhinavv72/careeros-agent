"""Verified candidate values, attempt artifacts and deterministic recovery policy."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
import uuid


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    try:
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
        # Windows sync/antivirus processes can briefly hold the destination.
        # Retry the atomic replace without ever writing a partial JSON file.
        for attempt in range(5):
            try:
                temp.replace(path)
                break
            except PermissionError:
                if attempt == 4:raise
                time.sleep(0.05 * (2 ** attempt))
    finally:
        try:temp.unlink(missing_ok=True)
        except PermissionError:pass


def candidate_values(profile, saved):
    p, e = profile.get('personal', {}), profile.get('experience', {})
    values = {}
    def put(keys, value):
        if value is not None and str(value).strip():
            for key in keys:
                values[key] = str(value)
    put(['full name', 'name'], ' '.join(str(p[k]) for k in ('firstName', 'lastName') if p.get(k)))
    for keys, field in ((['first name','firstname'],'firstName'), (['last name','lastname'],'lastName'),
                        (['email'],'email'), (['phone','mobile'],'phone'),
                        (['current location','location','city'],'city'), (['country'],'country'),
                        (['postal code','postal_code','zip code','zip_code','zip','pincode'],'postalCode'),
                        (['linkedin'],'linkedin'), (['github'],'github'), (['portfolio','website'],'portfolio')):
        put(keys, p.get(field))
    put(['current company','current employer'], e.get('currentCompany'))
    put(['current title','current designation'], e.get('currentTitle'))
    notice = saved.get('notice_period')
    if notice is None and e.get('noticePeriodDays') is not None:
        notice = f"{e['noticePeriodDays']} days"
    put(['notice period','how quickly can you join'], notice)
    put(['current ctc','current compensation'], saved.get('current_compensation'))
    put(['expected ctc','expected compensation'], saved.get('expected_compensation'))
    return values


def verified_bank_value(key, profile, saved):
    if key in saved and saved[key] is not None:
        return str(saved[key])
    p, e = profile.get('personal', {}), profile.get('experience', {})
    mapping = {'current_company':e.get('currentCompany'), 'current_title':e.get('currentTitle'),
               'designation':e.get('currentTitle'), 'current_location':p.get('city'),
               'current_location_city':p.get('city'), 'linkedin':p.get('linkedin'),
               'github':p.get('github'), 'portfolio':p.get('portfolio'),
               'postal_code': p.get('postalCode'),
               'notice_period': f"{e['noticePeriodDays']} days" if e.get('noticePeriodDays') is not None else None}
    value = mapping.get(key)
    return str(value) if value is not None else None


def recovery_guidance(status, reason, submitted=False):
    text = (reason or '').casefold()
    if status == 'Applied':
        return {'category':'applied', 'action':'View the employer receipt.', 'automatic_retry':False}
    if submitted or (status == 'ManualReview' and not text) or 'after submit' in text or 'confirmation' in text:
        return {'category':'submission_review', 'action':'Check the employer portal or confirmation email before taking another action.', 'automatic_retry':False}
    rules = [
        ('residence_requirement', r'employer requires current residence', 'Confirm that you meet the employer’s current-residence requirement before applying.'),
        ('expired', r'job (?:closed|expired|removed)|position.*(?:filled|closed)|no longer available|not accepting|http (?:404|410)', 'Remove this closed vacancy from the queue.'),
        ('human_challenge', r'captcha|otp|verification|challenge', 'Complete verification in the open browser, then Continue.'),
        ('login', r'login|sign.in|session expired', 'Sign in to the employer portal, then retry.'),
        ('resume', r'missing selected resume|resume.*(?:missing|invalid)|filenotfound', 'Select an existing PDF resume, then retry.'),
        ('missing_answers', r'missing required|profile.json|missing:', 'Save the missing profile facts or screening answers, then retry.'),
        ('adapter', r'ats.*enabled|submit control|unsupported|disabled|form unavailable|access denied', 'Inspect the form or update the permitted adapter/settings.'),
        ('retry_limit', r'attempt limit', 'Review repeated failures before starting another application.'),
        ('stopped', r'keyboardinterrupt|stop requested', 'Resume this application when ready.'),
        ('network', r'timeout|timed.out|err_connection|err_name_not_resolved|connectionreset|connection aborted|interrupted before submission', 'A delayed retry is scheduled when the attempt limit allows it.'),
    ]
    for category, pattern, action in rules:
        if re.search(pattern, text):
            return {'category':category, 'action':action, 'automatic_retry':category == 'network'}
    return {'category':'inspect' if reason else 'ready', 'action':'Inspect the attempt evidence.' if reason else 'Ready to process.', 'automatic_retry':False}


def retry_policy(settings):
    policy = settings.get('retry', {})
    return (max(1, min(int(policy.get('maxAttempts', 3)), 20)),
            max(1, min(int(policy.get('baseDelaySeconds', 60)), 86400)),
            max(1, min(int(policy.get('maxDelaySeconds', 3600)), 86400)))


def retry_time(settings, attempts, timestamp):
    maximum, base, cap = retry_policy(settings)
    if attempts >= maximum:
        return None
    return (datetime.fromisoformat(timestamp) + timedelta(seconds=min(cap, base * 2**max(0, attempts-1)))).isoformat(timespec='seconds')


def due(value, timestamp):
    try:
        return not value or datetime.fromisoformat(value) <= datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return False


def snapshot_resume(source, directory):
    source, directory = Path(source), Path(directory)
    if not source.is_file() or source.suffix.casefold() != '.pdf':
        raise ValueError('Selected resume is missing or invalid; select an existing PDF resume')
    content = source.read_bytes()
    if not content.startswith(b'%PDF-'):
        raise ValueError('Selected resume is invalid; select a PDF resume')
    target = directory/'resume.pdf'
    target.write_bytes(content)
    return target, {'filename':source.name, 'sha256':hashlib.sha256(content).hexdigest()}


def publish_latest(directory, screenshots, job_id):
    for suffix in ('filled.png','trace.zip','questions.json','receipt.json'):
        source = Path(directory)/suffix
        if source.is_file():
            target = Path(screenshots)/f'job-{job_id}-{suffix}'
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.pending')
            try:
                shutil.copyfile(source, temp)
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)


def unavailable_reason(text, http_status=None):
    if http_status in (404, 410):
        return f'Job closed or removed: HTTP {http_status}'
    match = re.search(r'\b(?:this (?:job|position|vacancy|role) (?:is |has been )?(?:closed|expired|filled|removed|no longer available)|'
                      r'(?:this (?:job|position)|we) (?:is |are )?no longer accepting applications|'
                      r'job (?:not found|has expired)|position has been filled)\b', text, re.I)
    return 'Job closed or expired: ' + match.group(0) if match else None


def wait_for_form_ready(surface, timeout=10000):
    surface.locator('input:visible:not([type=hidden]), textarea:visible, select:visible, button[type=submit]:visible').first.wait_for(state='visible',timeout=timeout)


def wait_for_upload(surface, timeout=10000):
    # Wait for active upload/parse indicators rather than an arbitrary sleep.
    progress = surface.locator('[role=progressbar]:visible, [aria-busy=true]:visible, .upload-progress:visible')
    # An empty locator must not spend the full timeout waiting for an element
    # that never existed. This was the dominant source of slow applications.
    if progress.count() == 0:
        return
    progress.first.wait_for(state='hidden',timeout=timeout)


def wait_for_form_change(surface, fingerprint, timeout=10000):
    frame = getattr(surface, 'frame', surface)
    if callable(frame):frame=surface  # Page.frame is a lookup method; FormSurface.frame is a Frame.
    frame.wait_for_function('''previous => {
        const fields = [...document.querySelectorAll('input,textarea,select')].filter(el => el.getClientRects().length);
        return JSON.stringify(fields.map(el => [el.name,el.type,el.id,[...(el.labels || [])].map(l => l.innerText).join(' '),el.getAttribute('aria-label')])) !== previous;
    }''', arg=fingerprint, timeout=timeout)
    wait_for_form_ready(surface, timeout)


def form_fingerprint(surface):
    return surface.locator('input:visible,textarea:visible,select:visible').evaluate_all("els => JSON.stringify(els.map(el => [el.name,el.type,el.id,[...(el.labels || [])].map(l => l.innerText).join(' '),el.getAttribute('aria-label')]))")
