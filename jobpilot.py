#!/usr/bin/env python3
"""CareerOS: local-first job discovery and application operations."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import socket
import sqlite3
import sys
import os
import time
import urllib.request
import urllib.parse
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, closing, contextmanager
from agent_runtime import exclusive_lock, recover_attempts
from application_support import due, retry_policy, retry_time
from profile_agent import answer as profile_answer
from ats_forms import ADAPTERS, GENERIC, prepare_application, inspect_fields, unresolved_fields, submission_control, next_application_control, collect_question_options, fill_typeahead_field, scroll_all_cards, posting_closed
from difflib import SequenceMatcher
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG = ROOT / "config"
DB = DATA / "jobpilot.db"
TRACKER = ROOT / "application-tracker.csv"
DISCOVERY_MAX_WORKERS = 6
NAUKRI_PROFILE = DATA / "browser" / "naukri"
APPLICATION_PROFILE = DATA / "browser" / "applications"
_JSON_CACHE = {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#.]+", " ", value.lower())).strip()


def plain_html(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def matching_option(answer: str, labels: list[str]) -> int | None:
    """Prefer an exact answer; accept only one unambiguous partial match."""
    target = norm(answer)
    if not target:
        return None
    normalized = [norm(label) for label in labels]
    exact = [i for i, label in enumerate(normalized) if label == target]
    if len(exact) == 1:
        return exact[0]
    if re.fullmatch(r'\d+(?:\.\d+)?',target):
        value=float(target)
        for i,label in enumerate(labels):
            number=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*years?\s*',label,re.I)
            if number and float(number[1])==value:return i
        ranges=[]
        for i,label in enumerate(labels):
            interval=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)?\s*',label,re.I)
            if interval and float(interval[1])<=value<=float(interval[2]):
                ranges.append((float(interval[2])-float(interval[1]),i))
        if ranges:return min(ranges)[1]
        for i,label in enumerate(labels):
            comparison=re.fullmatch(r'\s*(more than|less than|at least|at most)\s+(\d+(?:\.\d+)?)\s*(years?|months?)\s*',label,re.I)
            if comparison:
                threshold=float(comparison[2])/(12 if comparison[3].lower().startswith('month') else 1)
                operators={'more than':value>threshold,'less than':value<threshold,'at least':value>=threshold,'at most':value<=threshold}
                if operators[comparison[1].lower()]:return i
        return None
    partial = [i for i, label in enumerate(normalized) if label and (target in label or label in target)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        # Disambiguate if exactly one option specifies target India tech hub
        state_matches = [i for i in partial if "telangana" in normalized[i]]
        if len(state_matches) == 1:
            return state_matches[0]
        loc_matches = [i for i in partial if any(k in normalized[i] for k in ("india", "telangana", "karnataka", "hyderabad", "bengaluru", "bangalore"))]
        if len(loc_matches) == 1:
            return loc_matches[0]
        return None
    affirmative = {"yes", "i agree", "agree", "i consent", "consent", "true", "accepted", "i accept", "acknowledge", "i acknowledge"}
    if target in affirmative:
        negative_words = ("do not", "don't", "decline", "disagree", "refuse", "no")
        aff_matches = [i for i, label in enumerate(normalized) if (label in affirmative or any(a in label for a in ("consent", "agree", "accept", "acknowledge"))) and not any(neg in label for neg in negative_words)]
        if len(aff_matches) == 1:
            return aff_matches[0]
    return None


@dataclass
class Job:
    external_id: str
    company: str
    title: str
    description: str
    location: str
    source: str
    source_url: str
    apply_url: str
    ats: str
    posted_at: str = ""

    @property
    def fingerprint(self) -> str:
        raw = "|".join((norm(self.company), norm(self.title), norm(self.location)))
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def description_hash(self) -> str:
        return hashlib.sha256(norm(self.description).encode()).hexdigest()


def load_json(name: str):
    path = CONFIG / name
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if name not in _JSON_CACHE or _JSON_CACHE[name][0] != key:
        _JSON_CACHE[name] = (key, json.loads(path.read_text(encoding="utf-8")))
    return _JSON_CACHE[name][1]


def connect() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    initialize_schema = not DB.exists() or DB.stat().st_size == 0
    db = sqlite3.connect(DB, timeout=60)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=60000")
    except Exception:
        pass
    if initialize_schema:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs(
          id INTEGER PRIMARY KEY, external_id TEXT, company TEXT NOT NULL, title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', source TEXT,
          source_url TEXT, apply_url TEXT, ats TEXT, posted_at TEXT, discovered_at TEXT NOT NULL,
          fingerprint TEXT NOT NULL UNIQUE, description_hash TEXT, eligible INTEGER,
          eligibility_reason TEXT, score REAL, priority TEXT, resume TEXT, status TEXT NOT NULL DEFAULT 'Discovered');
        CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_apply_url ON jobs(apply_url) WHERE apply_url <> '';
        CREATE TABLE IF NOT EXISTS job_sources(job_id INTEGER NOT NULL, source TEXT NOT NULL, url TEXT NOT NULL,
          discovered_at TEXT NOT NULL, UNIQUE(job_id, url));
        CREATE TABLE IF NOT EXISTS application_queue(job_id INTEGER PRIMARY KEY, priority TEXT, score REAL,
          mode TEXT NOT NULL DEFAULT 'SEMI_AUTO', attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
          status TEXT NOT NULL DEFAULT 'Queued', error TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_attempts(id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL,
          started_at TEXT NOT NULL, completed_at TEXT, status TEXT, adapter TEXT, step TEXT, error TEXT, screenshot_path TEXT);
        CREATE TABLE IF NOT EXISTS unknown_questions(id INTEGER PRIMARY KEY, normalized_question TEXT UNIQUE,
          question TEXT NOT NULL, answer TEXT, job_id INTEGER, company TEXT, created_at TEXT NOT NULL);
        """)
    return db


def fetch_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "JobPilot/0.1 personal job search"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def greenhouse(company: dict) -> Iterable[Job]:
    board = company["boardId"]
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true")
    for item in data.get("jobs", []):
        yield Job(str(item["id"]), company["company"], item["title"], plain_html(item.get("content", "")),
                  item.get("location", {}).get("name", ""), "Greenhouse", item["absolute_url"],
                  item["absolute_url"], "greenhouse", item.get("first_published") or item.get("first_published_at") or "")


def lever(company: dict) -> Iterable[Job]:
    board = company["boardId"]
    data = fetch_json(f"https://api.lever.co/v0/postings/{board}?mode=json")
    for item in data:
        cats = item.get("categories", {})
        sections = [item.get("descriptionPlain") or item.get("description", "")]
        sections.extend(f"{section.get('text', '')} {section.get('content', '')}" for section in item.get("lists", []))
        sections.append(item.get("additionalPlain") or item.get("additional", ""))
        yield Job(str(item["id"]), company["company"], item["text"], plain_html(" ".join(sections)),
                  cats.get("location", ""), "Lever", item["hostedUrl"], item.get("applyUrl", item["hostedUrl"]),
                  "lever", datetime.fromtimestamp(item["createdAt"] / 1000, timezone.utc).isoformat() if item.get("createdAt") else "")


def ashby(company: dict) -> Iterable[Job]:
    board = company["boardId"]
    data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true")
    for item in data.get("jobs", []):
        yield Job(item.get("id", item["jobUrl"]), company["company"], item["title"],
                  plain_html(item.get("descriptionHtml", item.get("descriptionPlain", ""))),
                  item.get("location", ""), "Ashby", item["jobUrl"], item.get("applyUrl", item["jobUrl"]),
                  "ashby", item.get("publishedAt", ""))


def smartrecruiters(company: dict) -> Iterable[Job]:
    board = company["boardId"]
    data = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100")
    for item in data.get("content", []):
        loc = item.get("location", {})
        location = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
        url = f"https://jobs.smartrecruiters.com/{board}/{item['id']}"
        yield Job(item["id"], company["company"], item["name"], "", location, "SmartRecruiters", url, url,
                  "smartrecruiters", item.get("releasedDate", ""))


SOURCES = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters}


def ats_for(url: str) -> str:
    patterns = {"naukri": "naukri.com", "indeed": "indeed.com", "linkedin": "linkedin.com", "greenhouse": "greenhouse.io", "lever": "lever.co", "ashby": "ashbyhq.com",
                "workday": "myworkdayjobs.com", "smartrecruiters": "smartrecruiters.com", "icims": "icims.com",
                "oracle": "oraclecloud.com", "taleo": "taleo.net", "workable": "workable.com",
                "bamboohr": "bamboohr.com", "teamtailor": "teamtailor.com", "jobvite": "jobvite.com"}
    host = (urlparse(url).hostname or "").lower()
    return next((name for name, token in patterns.items() if host == token or host.endswith("." + token)), "generic")


def naukri_search_url(keywords: str, location: str, page: int = 1) -> str:
    """Build a normal Naukri search URL without depending on its cosmetic slug."""
    params = urllib.parse.urlencode({"k": keywords, "l": location, "sortBy": "date", "qpost": "3", "jobAge": "7", "experience": "3"})
    suffix = "" if page <= 1 else f"-{page}"
    return f"https://www.naukri.com/jobs-in-india{suffix}?{params}"


def _naukri_block_reason(page) -> str | None:
    url = page.url.lower()
    text = page.locator("body").inner_text(timeout=5000).lower()
    if blocking_challenge(page) or "challenge" in url:
        return "CAPTCHA/challenge detected"
    if "access denied" in text or "unusual traffic" in text:
        return "Naukri blocked automated access"
    return None


@contextmanager
def naukri_browser_lock(timeout: float = 0.0):
    """Serialize access to NAUKRI_PROFILE so discovery and apply never collide."""
    from agent_runtime import WorkerBusy, exclusive_lock
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            with exclusive_lock(DATA / "naukri-browser.lock"):
                yield
                return
        except WorkerBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def cmd_naukri_login(_: argparse.Namespace) -> None:
    """Create a reusable local Chrome profile. The user completes Naukri's own login/OTP."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright is not installed. Run: python -m pip install -r requirements.txt")
    NAUKRI_PROFILE.mkdir(parents=True, exist_ok=True)
    with naukri_browser_lock(timeout=5.0), sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(NAUKRI_PROFILE), channel="chrome", headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.naukri.com/nlogin/login", wait_until="domcontentloaded", timeout=30000)
        print("Complete Naukri login in the open browser. CareerOS stores the session only on this computer.")
        input("After the Naukri home page shows you are signed in, press Enter here to save and close: ")
        context.close()


def cmd_portal_login(args: argparse.Namespace) -> None:
    """Open the persistent application browser so normal portal sessions can be saved."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright is not installed. Run: python -m pip install -r requirements.txt")
    login_profile = NAUKRI_PROFILE if ats_for(args.url) == 'naukri' else APPLICATION_PROFILE
    login_profile.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        if login_profile == NAUKRI_PROFILE:
            stack.enter_context(naukri_browser_lock(timeout=5.0))
        pw = stack.enter_context(sync_playwright())
        context = pw.chromium.launch_persistent_context(
            str(login_profile), channel="chrome", headless=False,
            viewport={"width": 1365, "height": 900})
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
        print("Sign in normally in the open Chrome window. Cookies remain only in CareerOS's local application profile.")
        wait_for_user(page, "Complete sign-in in Chrome, then click Continue in the dashboard.")
        try: context.close()
        except Exception: pass


def cmd_naukri_discover(args: argparse.Namespace) -> dict:
    """Import Naukri search results using the candidate's normal signed-in browser session."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright is not installed. Run: python -m pip install -r requirements.txt")
    if not NAUKRI_PROFILE.exists():
        raise SystemExit("Naukri session is not set up. Run: python jobpilot.py naukri-login")
    found = added = duplicates = 0
    browser_args = []
    if getattr(args, "headless", False):
        browser_args.extend(["--window-position=-32000,-32000", "--window-size=1365,900", "--no-first-run"])
    with naukri_browser_lock(timeout=0.0), sync_playwright() as pw, closing(connect()) as db:
        context = pw.chromium.launch_persistent_context(
            str(NAUKRI_PROFILE), channel="chrome", headless=False,
            args=browser_args,
            viewport={"width": 1365, "height": 900})
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(10000)
        for page_number in range(1, args.pages + 1):
            page.goto(naukri_search_url(args.keywords, args.location, page_number),
                      wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            reason = _naukri_block_reason(page)
            if reason:
                raise SystemExit(f"{reason}. No bypass was attempted; try again later in visible mode.")
            items = page.locator("a[href*='/job-listings-']").evaluate_all(r"""anchors => {
                const seen = new Set(); const rows = [];
                for (const a of anchors) {
                    const url = (a.href || '').split('?')[0];
                    if (!url || seen.has(url)) continue; seen.add(url);
                    const card = a.closest('article, .srp-jobtuple-wrapper, .jobTuple, [class*=jobTuple]') || a.parentElement;
                    const pick = selectors => {
                        for (const s of selectors) { const e = card && card.querySelector(s); if (e && e.textContent.trim()) return e.textContent.trim(); }
                        return '';
                    };
                    rows.push({url, title: (a.title || a.textContent || '').trim(),
                        company: pick(['.comp-name','.subTitle','[class*=company]']),
                        location: pick(['.locWdth','.location','.loc-wrap','[class*=location]']),
                        description: pick(['.job-desc','.job-description','.row5','[class*=description]']),
                        card_text: card ? card.innerText.replace(/\s+/g, ' ').trim() : ''});
                } return rows;
            }""")
            for item in items:
                if not item.get("title"): continue
                found += 1
                description = "\n".join(value for value in (item.get("description", ""), item.get("card_text", "")) if value)
                job = Job(item["url"], item.get("company") or "Naukri employer", item["title"],
                          description, item.get("location", ""), "Naukri",
                          item["url"], item["url"], "naukri")
                before = db.total_changes
                db.execute("""INSERT OR IGNORE INTO jobs(external_id,company,title,description,location,source,source_url,apply_url,ats,posted_at,discovered_at,fingerprint,description_hash)
                  VALUES(:external_id,:company,:title,:description,:location,:source,:source_url,:apply_url,:ats,:posted_at,:discovered_at,:fingerprint,:description_hash)""",
                  asdict(job) | {"discovered_at": now(), "fingerprint": job.fingerprint, "description_hash": job.description_hash})
                if db.total_changes > before: added += 1
                else: duplicates += 1
            db.commit()
            if page_number < args.pages: page.wait_for_timeout(2000)
        context.close()
    print(f"Naukri results found: {found}\nNew unique: {added}\nDuplicates: {duplicates}")
    return {"fetched": found, "imported": added, "duplicates": duplicates}


def resume_for(text: str) -> str:
    text = norm(text)
    groups = [("security.pdf", ("security", "vulnerability", "cve", "siem")),
              ("platform-sre.pdf", ("sre", "kubernetes", "devops", "gcp", "azure", "terraform")),
              ("fullstack.pdf", ("angular", "react", "frontend", "full stack")),
              ("backend.pdf", (".net", "c#", "asp.net", "backend", "rest api", "microservice"))]
    ranked = [(sum(text.count(k) for k in keys), file) for file, keys in groups]
    count, file = max(ranked)
    return file if count else "generic.pdf"


def candidate_db():
    """Open the candidate knowledge base, importing config changes first."""
    import candidate_kb as kb
    db = kb.connect(DATA / "candidate.db")
    seed_path = CONFIG / "candidate-seed.json"
    revision = kb.IMPORT_VERSION + ":" + candidate_config_revision() + (str(seed_path.stat().st_mtime_ns) if seed_path.exists() else "")
    marker = kb.get_fact(db, "meta.import_revision")
    if not marker or marker.value != revision:
        def read(name):
            try: return load_json(name)
            except (OSError, ValueError): return {}
        seed = json.loads(seed_path.read_text(encoding="utf-8")) if seed_path.exists() else None
        kb.import_legacy(db, read("profile.json"), read("answers.json"), seed)
        kb.put_fact(db, "meta.import_revision", revision, source="jobpilot", verification=kb.DERIVED)
    return db


def question_answer(question: str, options=None, required=None, kind: str = "text") -> tuple[str | None, str | None]:
    """Resolve a form field from verified candidate facts only.

    Returns ``(answer, intent)``; answer is None when the field must be left empty or
    blocks the application. Blocks are recorded with what would unblock them.
    """
    import answer_engine as ae
    if not norm(question):
        return None, None
    db = candidate_db()
    try:
        ctx = ae.current_context()
        result = ae.resolve(ae.Field(question, list(options or []), required, kind), ctx, db)
        ae.note(result, question)
        if result.action == ae.BLOCK and ctx.job_id is not None:
            import candidate_kb as kb
            kb.record_blocker(db, ctx.job_id, question, result.intent, result.reason, result.needs)
    finally:
        db.close()
    return (result.answer, result.intent) if result.action == ae.FILL else (None, result.intent)


def eligible(job: sqlite3.Row, rules: dict) -> tuple[bool, str]:
    company = norm(job['company'] if 'company' in job.keys() else '')
    blocked = [norm(c) for c in rules.get('blockedCompanies', [])]
    if any(b and (b == company or b in company or company in b) for b in blocked):
        return False, f"Company is in blockedCompanies"
    text = f"{job['title']} {job['description']} {job['location']}".casefold()
    title = norm(job["title"])
    location = norm(job["location"])
    profile = load_json("profile.json")
    for keyword in rules.get("excludedRoleKeywords", []):
        if keyword and re.search(r"\b" + re.escape(norm(keyword)) + r"\b", title):
            return False, f"Role title contains excluded term: {keyword}"
    if re.search(r"\b(?:business analyst|product manager|product owner|project manager|program manager|hiring specialist|recruiter|recruitment|talent acquisition)\b", title):
        return False, "Role family mismatch"
    if re.search(r"\b(?:french|german|spanish|japanese|mandarin)[- ]speaking\b", title):
        return False, "Required language is not in candidate profile"
    if rules.get("requireDotnet") and not re.search(r"c#|\.net|asp.net|dotnet|dot net", norm(job['title'] + ' ' + job['description'])):
        return False, "C#/.NET stack not stated"
    if rules.get("remoteOnly"):
        if "remote" not in location or re.search(r"hybrid|on.?site", location):
            return False, "Fully remote location not confirmed"
    if rules.get("maxPostingHours") and not ('ats' in job.keys() and job['ats'] == 'naukri' and not job['posted_at']):
        # Naukri searches already ask for the last 7 days; its cards carry no machine-readable date.
        try:
            posted = datetime.fromisoformat(job['posted_at'].replace('Z', '+00:00'))
            if posted.tzinfo is None: return False, "Posting timezone not confirmed"
            age = (datetime.now(timezone.utc) - posted).total_seconds() / 3600
            if age < 0 or age > rules['maxPostingHours']: return False, "Outside requested posting window"
        except (ValueError, TypeError, KeyError, IndexError):
            return False, "Original posting date not confirmed"
    if re.search(r"sde iii|engineer iii|android|ios|sensors|firmware|embedded", title):
        return False, "Specialist or senior role outside configured target"
    if not rules.get("allowInternships", False) and re.search(r"\bintern(ship)?\b", title):
        return False, "Internship excluded"
    # Genuine over-level titles stay hard rejects for a 3-year candidate. Plain
    # "senior"/"sr" no longer rejects here (India titles inflate); score() applies
    # a penalty instead so these rank below exact-level matches but still apply.
    if re.search(r"\b(lead|staff|director|vice president|vp|principal|architect|head of|engineering manager|manager|distinguished|fellow)\b", title):
        return False, "Seniority mismatch"
    if re.search(r'\b(?:ruby|rails)\b', title):
        return False, "Role requires Ruby/Rails, which is not in the verified candidate skills"
    if re.search(r"\b(thermal|mechanical|electrical|technical support|customer support|solutions engineer|sales engineer|pre.?sales|support engineer)\b", title):
        return False, "Role family mismatch"
    if not re.search(r"(?:software|backend|back.?end|front.?end|full.?stack|\.net|dot.?net|developer|development|programmer|engineer|sde|devops|platform|cloud|sre|security|application)", title):
        return False, "Role title mismatch"
    experience_cap = int(rules.get("maxExperienceYears", 5))
    candidate_years = profile.get("experience", {}).get("years")
    candidate_years = int(candidate_years) if candidate_years is not None else experience_cap
    # A "senior"/"sr" title is admitted only when the posting states an experience
    # level the candidate actually meets; a bare senior title with no stated range
    # stays a seniority mismatch. score() still ranks these below exact-level roles.
    seniorish = bool(re.search(r"\b(?:senior|sr\.?)\b", title))
    experience_confirmed = False
    preferred = re.compile(r"\b(?:preferred|preferably|nice to have|bonus|desirable|advantage|ideally)\b", re.I)
    remaining = text
    for match in re.finditer(r"\b(\d+)\s*(?:to|[-–])\s*(\d+)\s*(?:years?|yrs?)\b", text):
        context = text[max(0, match.start()-45):min(len(text), match.end()+30)]
        if preferred.search(context):
            continue
        minimum, maximum = int(match.group(1)), int(match.group(2))
        if minimum > candidate_years:
            return False, f"Requires at least {minimum} years; candidate profile has {candidate_years}"
        if maximum > experience_cap:
            return False, f"Posting range extends beyond {experience_cap} years"
        experience_confirmed = True
        remaining = remaining.replace(match.group(0), " ", 1)
    for match in re.finditer(r"\b(\d+)\s*\+?\s*(?:years?|yrs?)\b", remaining):
        context = remaining[max(0, match.start()-45):min(len(remaining), match.end()+30)]
        if preferred.search(context):
            continue
        required_years = int(match.group(1))
        if required_years > experience_cap or required_years > candidate_years:
            return False, f"Requires at least {required_years} years; candidate profile has {candidate_years}"
        experience_confirmed = True
    if seniorish and not experience_confirmed:
        return False, "Seniority mismatch"
    india = any(k in location for k in ("india", "bengaluru", "bangalore", "hyderabad", "pune", "gurugram", "gurgaon", "noida", "delhi", "mumbai", "chennai"))
    remote_anywhere = bool(re.search(r"\b(?:worldwide|anywhere|global|apac|asia)\b", location))
    if rules.get("indiaOnlyUnlessRemote", True) and not india:
        if "remote" not in location:
            return False, "No India or India-eligible remote location signal"
        if not remote_anywhere:
            # Any named place left after removing generic work-mode words
            # (e.g. "Remote Spain", "Remote - US") scopes the role outside India.
            leftover = re.sub(
                r"\b(?:remote|fully|full|time|part|work|from|home|wfh|first|friendly|only|based|"
                r"flexible|optional|or|and|in|the|position|role|job)\b|[^a-z]+", " ", location).split()
            if leftover:
                return False, "Remote role is restricted to a region outside India"
            return False, "Remote country scope not confirmed"
    return True, "Eligible"


def score(job: sqlite3.Row, profile: dict, skills: dict) -> float:
    text = norm(f"{job['title']} {job['description']} {job['location']}")
    title = norm(job["title"])
    roles = [norm(x) for x in profile["preferences"]["roles"]]
    role_score = max((len(set(title.split()) & set(role.split())) / max(1, len(set(role.split()))) for role in roles), default=0) * 30
    weighted = skills.get("weighted", {})
    present = sum(float(weight) for skill, weight in weighted.items() if norm(skill) in text)
    total = max(1.0, sum(float(x) for x in weighted.values()))
    skill_score = min(25, 25 * present / (total * 0.35))
    location_score = 10 if any(norm(x) in text for x in profile["preferences"]["locations"]) else 0
    broad_role = 10 if any(x in title for x in ("software", "developer", "engineer", "sre")) else 0
    freshness = 7
    experience = 15
    # Near-miss seniority is allowed through eligible() but ranked lower here so
    # exact-level roles queue first.
    seniority_penalty = 12 if re.search(r"\b(?:senior|sr\.?|architect)\b", title) else 0
    return round(min(100, max(0.0, role_score + skill_score + location_score + broad_role + freshness + experience - seniority_penalty)), 1)


@dataclass
class ScoreBreakdown:
    role_score: float = 0.0
    skill_score: float = 0.0
    location_score: float = 0.0
    matched_required_skills: list[str] = field(default_factory=list)
    missing_required_skills: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)


@dataclass
class MatchDecision:
    score: float
    explanation: str = ""
    score_breakdown: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    recommended_resume: str = "generic.pdf"
    eligibility_reason: str = "Qualified by profile rules"


def match_decision(job, profile=None, skills=None, rules=None) -> MatchDecision:
    profile = profile or load_json("profile.json")
    skills = skills or load_json("skills.json")
    rules = rules or load_json("eligibility.json")
    s = score(job, profile, skills)
    resume = job["resume"] if ("resume" in job.keys() and job["resume"]) else profile.get("resumes", {}).get("default", "generic.pdf")
    text = norm(f"{job['title']} {job['description']}")
    weighted = skills.get("weighted", {})
    matched = [sk for sk in weighted if norm(sk) in text]
    missing = [sk for sk in weighted if norm(sk) not in text][:3]
    breakdown = ScoreBreakdown(
        role_score=s * 0.4,
        skill_score=s * 0.4,
        location_score=s * 0.2,
        matched_required_skills=matched,
        missing_required_skills=missing,
        uncertainties=[]
    )
    explanation = f"Match score {s:.0f}/100 based on title and {len(matched)} matched skill(s)."
    return MatchDecision(score=s, explanation=explanation, score_breakdown=breakdown, recommended_resume=resume)


def priority(value: float) -> str:
    return "P0" if value >= 85 else "P1" if value >= 75 else "P2" if value >= 60 else "P3"


def cmd_init(_: argparse.Namespace) -> None:
    db = connect()
    for path in (DATA / "resumes", DATA / "screenshots", DATA / "exports"):
        path.mkdir(parents=True, exist_ok=True)
    imported = 0
    if TRACKER.exists():
        with TRACKER.open(newline="", encoding="utf-8-sig") as file:
            for item in csv.DictReader(file):
                url = item.get("job_link_or_id", "")
                job = Job(url, item.get("employer", "Unknown"), item.get("role", "Unknown"),
                          item.get("required_follow_up", ""), item.get("location", ""),
                          item.get("platform", "Imported report"), url if url.startswith("http") else "", url if url.startswith("http") else "", ats_for(url))
                before = db.total_changes
                db.execute("""INSERT OR IGNORE INTO jobs(external_id,company,title,description,location,source,source_url,apply_url,ats,posted_at,discovered_at,fingerprint,description_hash,status,eligibility_reason,resume)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'ManualReview',?,?)""",
                  (job.external_id,job.company,job.title,job.description,job.location,job.source,job.source_url,job.apply_url,job.ats,"",now(),job.fingerprint,job.description_hash,item.get("application_status", "Imported from report"),item.get("resume_version", "generic.pdf")))
                imported += db.total_changes > before
    db.commit(); db.close()
    print(f"Initialized {DB}\nImported existing tracker rows: {imported}")


def wait_for_application_write_window() -> None:
    """Yield discovery writes while an application owns the browser worker."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if (DATA / 'discovery-paused').exists():
            time.sleep(2)
            continue
        try:
            with sqlite3.connect(DB, timeout=5) as check:
                active = check.execute("SELECT 1 FROM agent_runs WHERE action='apply' AND status='Running' LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            return
        if not active:
            return
        time.sleep(2)


def cmd_discover(args: argparse.Namespace | None) -> dict:
    """Fetch a bounded, rotating group of employer feeds concurrently; keep DB writes serial."""
    companies = [company for company in load_json("companies.json")
                 if company.get("enabled", True) and company.get("ats") in SOURCES]
    if not companies:
        print("No enabled employer boards are configured.")
        return {"succeeded": 0, "errors": 0}
    state_path = DATA / "scheduler-state.json"
    try:
        schedule_state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        schedule_state = {}
    requested_limit = getattr(args, "limit", None) if args is not None else None
    limit = len(companies) if requested_limit is None or int(requested_limit) <= 0 else min(int(requested_limit), len(companies))
    offset = int(getattr(args, "offset", schedule_state.get("employer_board_offset", 0)) or 0) % len(companies)
    selected = [companies[(offset + index) % len(companies)] for index in range(limit)]
    on_batch = getattr(args, "on_batch", None) if args is not None else None
    for company in selected:
        print(f"Fetching {company['company']} ({company['ats']})", flush=True)

    found = added = duplicates = errors = 0
    db = connect()
    try:
        with ThreadPoolExecutor(max_workers=min(DISCOVERY_MAX_WORKERS, len(selected))) as pool:
            pending = {pool.submit(lambda item: list(SOURCES[item["ats"]](item)), company): company
                       for company in selected}
            # Futures fetch independently. Results are written in a single thread
            # so concurrent source responses cannot contend on SQLite.
            for future in as_completed(pending):
                check_stop()
                if os.environ.get('JOBPILOT_DISCOVERY_ONLY'):
                    wait_for_application_write_window()
                company = pending[future]
                try:
                    jobs = future.result()
                    batch_started = now()
                    print(f"{company['company']}: fetched {len(jobs)} postings", flush=True)
                    for job in jobs:
                        check_stop()
                        found += 1
                        cursor = db.execute("""INSERT OR IGNORE INTO jobs(external_id,company,title,description,location,source,source_url,apply_url,ats,posted_at,discovered_at,fingerprint,description_hash)
                          VALUES(:external_id,:company,:title,:description,:location,:source,:source_url,:apply_url,:ats,:posted_at,:discovered_at,:fingerprint,:description_hash)""",
                          asdict(job) | {"discovered_at": now(), "fingerprint": job.fingerprint, "description_hash": job.description_hash})
                        if cursor.rowcount:
                            added += 1
                            job_id = cursor.lastrowid
                        else:
                            duplicates += 1
                            row = db.execute("SELECT id,description_hash,posted_at FROM jobs WHERE fingerprint=? OR apply_url=? LIMIT 1", (job.fingerprint, job.apply_url)).fetchone()
                            job_id = row['id'] if row else None
                            if row and ((job.description_hash and job.description_hash != row['description_hash']) or (job.posted_at and job.posted_at != row['posted_at'])):
                                db.execute("UPDATE jobs SET description=?,description_hash=?,posted_at=? WHERE id=?", (job.description, job.description_hash, job.posted_at or row['posted_at'], job_id))
                        if job_id:
                            db.execute("INSERT OR IGNORE INTO job_sources VALUES(?,?,?,?)", (job_id, job.source, job.source_url, now()))
                        if os.environ.get('JOBPILOT_DISCOVERY_ONLY') and found % 25 == 0:
                            db.commit()
                            wait_for_application_write_window()
                    db.commit()
                    if on_batch:
                        try:
                            on_batch(batch_started, company)
                        except Exception as exc:
                            print(f"{company['company']}: post-fetch processing failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                except Exception as exc:
                    db.rollback()
                    errors += 1
                    print(f"{company['company']}: {exc}", file=sys.stderr, flush=True)
    finally:
        db.close()

    if limit < len(companies):
        DATA.mkdir(parents=True, exist_ok=True)
        schedule_state["employer_board_offset"] = (offset + limit) % len(companies)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(schedule_state), encoding="utf-8")
        temporary.replace(state_path)
    print(f"Employer boards checked: {len(selected)}\nFound: {found}\nNew unique: {added}\nDuplicates: {duplicates}\nSource errors: {errors}", flush=True)
    return {"succeeded": len(selected) - errors, "errors": errors, "fetched": found,
            "imported": added, "duplicates": duplicates}


def cmd_score(args: argparse.Namespace | None) -> None:
    profile = load_json("profile.json"); rules = load_json("eligibility.json"); skills = load_json("skills.json")
    discovered_after = getattr(args, "discovered_after", None) if args is not None else None
    limit = getattr(args, "limit", None) if args is not None else None
    # A skipped listing was already rejected or failed live verification.
    # Re-scoring it on every feed refresh loops the same bad application.
    where = """status IN ('Discovered','Qualified','Queued') AND NOT EXISTS
               (SELECT 1 FROM application_queue q WHERE q.job_id=jobs.id AND q.mode='USER_LINK')"""
    parameters = []
    if discovered_after:
        where += " AND discovered_at>=?"
        parameters.append(discovered_after)
    query = f"SELECT * FROM jobs WHERE {where} ORDER BY discovered_at DESC"
    if limit and int(limit) > 0:
        query += " LIMIT ?"
        parameters.append(int(limit))
    qualified = 0
    write_seconds = 0.0
    db = connect()
    try:
        rows = db.execute(query, parameters).fetchall()
        for offset in range(0, len(rows), 50):
            updates = []
            for job in rows[offset:offset + 50]:
                ok, reason = eligible(job, rules)
                value = score(job, profile, skills) if ok else 0
                passed = ok and value >= rules.get("minimumScore", 50)
                status = job['status'] if passed and job['status'] == 'Queued' else (
                    'Qualified' if passed else 'Skipped')
                qualified += passed
                updates.append((ok, reason, value, priority(value) if value else None,
                                resume_for(job['title']+' '+job['description']), status, job))
            started = time.monotonic()
            with db:
                for ok, reason, value, rank, resume, status, job in updates:
                    updated = db.execute("""UPDATE jobs SET eligible=?,eligibility_reason=?,score=?,priority=?,resume=?,status=?
                        WHERE id=? AND status=? AND NOT EXISTS (SELECT 1 FROM application_queue q
                        WHERE q.job_id=jobs.id AND q.mode='USER_LINK')
                        AND NOT EXISTS (SELECT 1 FROM application_attempts a
                        WHERE a.job_id=jobs.id AND (a.completed_at IS NULL OR a.step='submit'))""",
                        (ok, reason, value, rank, resume, status, job['id'], job['status']))
                    if updated.rowcount and job['status'] == 'Queued':
                        if status == 'Skipped':
                            db.execute("""UPDATE application_queue SET status='Skipped',error=?
                                WHERE job_id=? AND status='Queued'""", (reason, job['id']))
                        else:
                            db.execute("""UPDATE application_queue SET score=?,priority=?
                                WHERE job_id=? AND status='Queued'""", (value, rank, job['id']))
            write_seconds += time.monotonic() - started
    finally:
        db.close()
    print(f"Scored: {len(rows)}\nQualified: {qualified}\nScore database write time: {write_seconds:.3f}s")


def cmd_queue(args: argparse.Namespace) -> None:
    db = connect(); target = args.limit or load_json("settings.json")["dailyTarget"]
    discovered_after = getattr(args, "discovered_after", None)
    settings = load_json("settings.json")
    allowed = [str(ats).lower() for ats in settings.get("allowedAutoSubmitAts", [])]
    from ats_forms import supports_autosubmit
    from application_support import retry_policy
    allowed = [ats for ats in allowed if supports_autosubmit(ats)]
    max_attempts, _, _ = retry_policy(settings)
    where = """status='Qualified'
        AND NOT EXISTS (SELECT 1 FROM application_attempts a WHERE a.job_id=jobs.id AND (a.step='submit' OR a.completed_at IS NULL))
        AND NOT EXISTS (SELECT 1 FROM application_queue q WHERE q.job_id=jobs.id AND q.attempts>=?)"""
    parameters = [max_attempts]
    if discovered_after:
        where += " AND discovered_at>=?"
        parameters.append(discovered_after)
    if allowed:
        placeholders = ','.join('?' for _ in allowed)
        where += f" AND LOWER(COALESCE(ats,'')) IN ({placeholders})"
        parameters.extend(allowed)
    if settings.get("unattended") and settings.get("autoSubmit"):
        auto_min = float(settings.get("autoApplyMinimumScore", 0) or 0)
        if auto_min > 0:
            where += " AND COALESCE(score,0)>?"
            parameters.append(auto_min)
    rows = db.execute(f"SELECT id,priority,score FROM jobs WHERE {where} ORDER BY score DESC LIMIT ?", (*parameters, target)).fetchall()
    with db:
        for row in rows:
            db.execute("""INSERT INTO application_queue(job_id,priority,score,status,error,created_at)
                VALUES(?,?,?,'Queued',NULL,?)
                ON CONFLICT(job_id) DO UPDATE SET priority=excluded.priority,score=excluded.score,status='Queued',error=NULL""",
                (*row, now()))
            db.execute("UPDATE jobs SET status='Queued' WHERE id=?", (row["id"],))
        if allowed:
            unsupported = db.execute(f"SELECT id, ats FROM jobs WHERE status='Qualified' AND LOWER(COALESCE(ats,'')) NOT IN ({placeholders})", allowed).fetchall()
            for u in unsupported:
                db.execute("UPDATE jobs SET status='ManualReview',eligibility_reason=? WHERE id=?",
                           (f"{u['ats'] or 'Portal'} has no validated submit-and-confirm adapter.", u["id"]))
    db.close(); print(f"Queued: {len(rows)}")


def cmd_export(_: argparse.Namespace) -> None:
    db = connect(); rows = db.execute("SELECT * FROM jobs ORDER BY discovered_at DESC").fetchall()
    DATA.joinpath("exports").mkdir(parents=True, exist_ok=True)
    path = DATA / "exports" / "JobApplications.csv"
    headers = ["JobId","DateFound","DateApplied","Company","Role","Location","MatchScore","Priority","Source","ATS","JobUrl","ApplyUrl","Resume","Mode","Status","Notes"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f); writer.writerow(headers)
        for r in rows: writer.writerow([r['id'],r['discovered_at'],"",r['company'],r['title'],r['location'],r['score'],r['priority'],r['source'],r['ats'],r['source_url'],r['apply_url'],r['resume'],"SEMI_AUTO",r['status'],r['eligibility_reason']])
    db.close(); print(f"Exported: {path}")


def cmd_status(_: argparse.Namespace) -> None:
    db = connect(); rows = db.execute("SELECT status,COUNT(*) count FROM jobs GROUP BY status ORDER BY status").fetchall()
    print("CareerOS status"); [print(f"{r['status']}: {r['count']}") for r in rows]
    queued = db.execute("""SELECT COUNT(*) FROM application_queue q
        JOIN jobs j ON j.id=q.job_id WHERE q.status='Queued' AND j.status='Queued'""").fetchone()[0]
    print(f"Active application queue: {queued}\nDatabase: {DB}"); db.close()


def safe_screenshot(page, path: str | Path) -> None:
    """Screenshot with verification-code boxes masked, including inside employer iframes."""
    selectors = ('input[id^="security-input-"]', 'input[autocomplete="one-time-code"]')
    try:
        frames = list(getattr(page, 'frames', None) or [])
        masks = [ctx.locator(sel) for ctx in [page, *frames] for sel in selectors]
        page.screenshot(path=str(path), timeout=3000, mask=masks)
        return
    except Exception:
        pass
    # A detached frame must not cost us the evidence screenshot; fall back to page masks.
    try: page.screenshot(path=str(path), timeout=3000, mask=[page.locator(sel) for sel in selectors])
    except Exception: pass


def visible_page_text(page) -> str:
    """Rendered body text only; unlike locator counts this excludes hidden login/OTP templates."""
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def blocking_challenge(page) -> bool:
    """Detect an active challenge, not a passive reCAPTCHA badge/anchor."""
    challenge = page.locator("""
        iframe[title*='challenge' i]:visible,
        iframe[src*='/challenge/']:visible,
        iframe[src*='hcaptcha.com/captcha']:visible,
        [class*='captcha-challenge' i]:visible,
        [id*='captcha-challenge' i]:visible
    """)
    text = visible_page_text(page)
    return bool(challenge.count()) or bool(re.search(
        # "Security Check" / "Just a moment" are Cloudflare interstitials whose
        # "Verify you are human" text lives inside a cross-origin iframe.
        r"verify (?:that )?you are human|complete the captcha|security challenge|unusual traffic|"
        r"\bsecurity check\b|just a moment\.\.\.|checking your browser|performing security verification|"
        r"verifies you are not a bot",
        text, re.I))


def wait_for_manual_challenge(page, unattended: bool = False, company: str = "", job_id: int | str = "") -> bool:
    """Let automated or interactive solver resolve challenges, then recheck before filling."""
    if not blocking_challenge(page):
        return True
    if unattended:
        return False
    try:
        from captcha_solver import is_captcha_present, solve_recaptcha_audio
        if solve_recaptcha_audio(page):
            return True
    except Exception:
        pass
    wait_for_user(page, "Complete CAPTCHA in Chrome, then click Continue. The app will recheck verification.")
    return not blocking_challenge(page)


def check_stop():
    path=os.environ.get('JOBPILOT_STOP_FILE')
    if path and Path(path).exists():raise KeyboardInterrupt('Stop requested in dashboard')


def open_application(page, url):
    """Retry only initial navigation, never a submission click."""
    for attempt in range(3):
        check_stop()
        try:
            page.goto(url,wait_until='domcontentloaded',timeout=30000)
            return
        except Exception as exc:
            if attempt==2 or not re.search(r'ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_RESET|ERR_TIMED_OUT|Timeout',str(exc),re.I):raise
            print(f'Initial navigation failed; retry {attempt+1}/2: {type(exc).__name__}',flush=True)
            page.wait_for_timeout(1000*(attempt+1))


def wait_for_user(page, message):
    if not os.environ.get('JOBPILOT_DASHBOARD'):
        input(message + ' Press Enter here when ready: ')
        return
    job_id=os.environ.get('JOBPILOT_CURRENT_JOB')
    if job_id:
        DATA.joinpath('screenshots').mkdir(parents=True,exist_ok=True)
        safe_screenshot(page,DATA/'screenshots'/f'job-{job_id}-filled.png')
    print('Needs your attention: '+message,flush=True)
    flag=Path(os.environ['JOBPILOT_CONTINUE_FILE'])
    deadline=time.monotonic()+600
    while time.monotonic()<deadline:
        check_stop()
        if flag.exists():
            flag.unlink()
            return
        page.wait_for_timeout(1000)
    raise TimeoutError('User verification was not completed within ten minutes')


VERIFICATION_TEXT = re.compile(r"verification code|security code|one.time (?:pass ?code|password|code)|\botp\b|enter the code", re.I)
CODE_INPUT_SELECTORS = ('input[id^="security-input-"]:visible', 'input[autocomplete="one-time-code"]:visible')
VERIFICATION_REJECTED = re.compile(
    r"error processing your application|invalid (?:security |verification )?code|"
    r"code (?:is |was )?(?:incorrect|invalid|expired)|please try again", re.I)
SUBMISSION_ACCEPTED = re.compile(r"thank you for (?:applying|your application)|application (?:has been )?(?:received|submitted)", re.I)


def email_verification_inputs(page, surface):
    """Locate segmented code inputs on the form surface, the page, or any employer iframe.

    Inputs only count when the same context's visible text asks for a code, so an
    unrelated one-time-code field on an ordinary form is never treated as an OTP step.
    """
    contexts = []
    for ctx in [surface, page, *list(getattr(page, 'frames', None) or [])]:
        if ctx is not None and not any(ctx is seen for seen in contexts):
            contexts.append(ctx)
    for ctx in contexts:
        for selector in CODE_INPUT_SELECTORS:
            inputs = ctx.locator(selector)
            if inputs.count() and VERIFICATION_TEXT.search(visible_page_text(ctx) or ''):
                return ctx, inputs
    return None, None


def _type_verification_code(fields, code):
    """Enter one character per box with real key events, as a person typing would.

    Segmented code widgets often record keystrokes rather than the input's value, so
    setting values directly can leave the widget's internal code empty.
    """
    for field, char in zip(fields, code):
        field.click()
        field.press_sequentially(char, delay=40)


def _await_verification_result(page, context, timeout_ms=15000):
    """After resubmitting with a code, report what the employer page actually did."""
    waited = 0
    while waited < timeout_ms:
        text = f"{visible_page_text(context)}\n{visible_page_text(page)}"
        if SUBMISSION_ACCEPTED.search(text):
            return 'accepted', None
        rejected = VERIFICATION_REJECTED.search(text)
        if rejected:
            return 'rejected', rejected.group(0)
        page.wait_for_timeout(500)
        waited += 500
    return 'unknown', None


def complete_email_verification(page, surface, submit):
    context, inputs = email_verification_inputs(page, surface)
    if inputs is None:
        return
    field_count = inputs.count()
    started = time.time()
    job_id = os.environ.get('JOBPILOT_CURRENT_JOB')
    if job_id:
        safe_screenshot(page, DATA / 'screenshots' / f'job-{job_id}-filled.png')
    print(f'Email verification required ({field_count} characters). Checking Gmail automatically...', flush=True)

    code_path = None
    continue_path = None
    if os.environ.get('JOBPILOT_CONTINUE_FILE'):
        code_path = Path(os.environ['JOBPILOT_CONTINUE_FILE']).with_suffix('.code')
        continue_path = Path(os.environ['JOBPILOT_CONTINUE_FILE'])

    deadline = time.monotonic() + 300
    last_gmail_check = 0.0

    while time.monotonic() < deadline:
        check_stop()
        code = None
        if code_path and code_path.exists():
            try:
                code = code_path.read_text(encoding='utf-8').strip()
                code_path.unlink(missing_ok=True)
            except Exception:
                pass

        if not code and (time.monotonic() - last_gmail_check >= 4.0):
            last_gmail_check = time.monotonic()
            try:
                import gmail_verifier
                # Only codes sent after this verification step began (with clock-skew slack),
                # so a code meant for an earlier attempt is never reused.
                code = gmail_verifier.fetch_latest_verification_code(
                    expected_length=field_count, timeout_seconds=6, since_timestamp=started - 120)
            except Exception as e:
                print(f'[verification] Gmail fetch notice: {e}', flush=True)

        if code:
            fields = inputs.all()
            if len(code) == len(fields):
                print(f'Applying a {len(code)}-character verification code to the security form inputs...', flush=True)
                try:
                    page.context.tracing.stop(path=str(DATA / 'screenshots' / f'job-{job_id}-trace.zip'))
                except Exception:
                    pass
                _type_verification_code(fields, code)
                page.wait_for_timeout(500)
                check_stop()
                if submit.is_enabled():
                    posts = []

                    def record_post(response):
                        try:
                            if response.request.method == 'POST':
                                posts.append(f"{response.status} {urlparse(response.url).path[:60]}")
                        except Exception:
                            pass
                    try:
                        page.on('response', record_post)
                    except Exception:
                        pass
                    submit.click(no_wait_after=True)
                    print('Verification code submitted; checking employer confirmation.', flush=True)
                    outcome, detail = _await_verification_result(page, context)
                    try:
                        page.remove_listener('response', record_post)
                    except Exception:
                        pass
                    if posts:
                        print(f"[verification] Resubmit POST responses: {', '.join(posts[-5:])}", flush=True)
                    if outcome == 'rejected':
                        print(f'[verification] Employer rejected the resubmission after the code: "{detail}"', flush=True)
                        if job_id:
                            safe_screenshot(page, DATA / 'screenshots' / f'job-{job_id}-verification-rejected.png')
                    elif outcome == 'unknown':
                        print('[verification] No acceptance or error shown within 15 seconds of resubmitting.', flush=True)
                    return
                else:
                    print('Verification submit button not yet enabled, retrying...', flush=True)
            else:
                print(f"Verification code length ({len(code)}) does not match fields ({len(fields)}).", flush=True)

        if continue_path and continue_path.exists():
            try:
                continue_path.unlink(missing_ok=True)
            except Exception:
                pass
            return

        page.wait_for_timeout(1000)

    raise TimeoutError('Email verification not completed within five minutes')


def naukri_page_metadata(page) -> str:
    """Read job-specific metadata that Naukri embeds in the page title and description."""
    parts = []
    try:
        title = page.title()
        if isinstance(title, str) and title.strip():
            parts.append(title.strip())
    except Exception:
        pass
    try:
        description = page.locator("meta[name='description']").get_attribute('content')
        if isinstance(description, str) and description.strip():
            parts.append(description.strip())
    except Exception:
        pass
    return " ".join(parts)


def save_application_receipt(db: sqlite3.Connection, job: sqlite3.Row, confirmation_text: str, screenshot: Path,
                             confirmation_url: str = "") -> Path:
    """Persist receipt evidence only after portal confirmation is visible."""
    attempt = db.execute("SELECT id FROM application_attempts WHERE job_id=? AND completed_at IS NULL ORDER BY id DESC LIMIT 1",
                         (job['id'],)).fetchone()
    if not attempt:
        raise RuntimeError(f"No active application attempt exists for confirmed job {job['id']}")
    from application_support import atomic_json
    directory = DATA / "screenshots" / f"job-{job['id']}" / f"attempt-{attempt['id']}"
    directory.mkdir(parents=True, exist_ok=True)
    receipt_path = directory / "receipt.json"
    atomic_json(receipt_path, {
        "job_id": job['id'], "attempt_id": attempt['id'],
        "company": job['company'], "title": job['title'], "portal": job['ats'],
        "apply_url": job['apply_url'], "confirmation_url": confirmation_url,
        "confirmed_at": now(), "confirmation_text": (confirmation_text or "")[:1000],
        "screenshot_path": str(screenshot), "status": "Applied",
    })
    return receipt_path


def naukri_direct_apply_control(page):
    """Find a visible Naukri Apply control without selecting employer redirects."""
    selector = "#apply-button:visible, button:visible, a:visible, [role='button']:visible"
    try:
        controls = page.locator(selector).filter(has_text=re.compile(r"^\s*apply\s*$", re.I)).all()
    except Exception:
        controls = []
    for control in controls:
        try:
            href = control.get_attribute('href')
        except Exception:
            href = None
        host = (urlparse(href).hostname or '').lower() if href else ''
        if host and not host.endswith('naukri.com'):
            continue
        return control
    # Lightweight fallback for older Naukri markup and saved test fixtures.
    try:
        candidates = page.locator(selector).all()
    except Exception:
        candidates = []
    for candidate in candidates:
        try:
            href = candidate.get_attribute('href')
            label = candidate.inner_text().strip() if hasattr(candidate, 'inner_text') else ''
        except Exception:
            continue
        host = (urlparse(href).hostname or '').lower() if href else ''
        if host and not host.endswith('naukri.com'):
            continue
        if re.fullmatch(r"\s*apply\s*", label, re.I) or (not label and href and 'apply' in href.lower()):
            return candidate
    return None


def naukri_quota_file() -> Path:
    return DATA / "naukri-quota.json"


def record_naukri_quota_exhausted() -> str:
    """Pause Naukri-direct applies until just after the next IST midnight reset."""
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    next_reset = (now_ist + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0).astimezone(timezone.utc)
    resets_after = next_reset.isoformat(timespec="seconds")
    from application_support import atomic_json
    atomic_json(naukri_quota_file(), {"exhausted_at": now(), "resets_after": resets_after})
    return resets_after


def naukri_quota_active() -> bool:
    path = naukri_quota_file()
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        resets_after = str(data.get("resets_after") or "")
        if resets_after and datetime.fromisoformat(resets_after) > datetime.now(timezone.utc):
            return True
    except (OSError, ValueError, TypeError):
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def try_naukri_direct_apply(page, db: sqlite3.Connection, job: sqlite3.Row, screenshot: Path) -> None:
    """Submit only Naukri's signed-in direct Apply flow; never follow external forms."""
    body = visible_page_text(page)
    if re.search(r"access denied|permission to access", body, re.I):
        reason = "Naukri denied automated browser access"
        safe_screenshot(page, screenshot)
        db.execute("UPDATE jobs SET status='SkippedBlocked',eligibility_reason=? WHERE id=?", (reason, job["id"]))
        db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=?", (reason, job["id"])); db.commit()
        print(f"Skipped job {job['id']}: {reason}")
        return
    if re.search(r"you have already applied|application sent|applied successfully|applied to [\"']", body, re.I):
        safe_screenshot(page, screenshot)
        save_application_receipt(db, job, body, screenshot, page.url)
        db.execute("UPDATE jobs SET status='Applied',eligibility_reason='Naukri already applied/confirmed' WHERE id=?", (job["id"],))
        db.execute("UPDATE application_queue SET status='Applied',error=NULL WHERE job_id=?", (job["id"],)); db.commit()
        print(f"Applied: job {job['id']} — {job['company']} — {job['title']}")
        return
    if re.search(r"login to apply|register to apply|sign in to apply", body, re.I):
        reason = "Naukri login session expired"
    elif re.search(r"your daily quota has been expired|daily apply quota", body, re.I):
        reason = "Naukri daily apply quota expired"
        record_naukri_quota_exhausted()
    else:
        apply_control = naukri_direct_apply_control(page)
        if not apply_control:
            reason = "Naukri direct Apply button not available"
        else:
            before_url = page.url
            mark_submission_started(db, job["id"])
            from playwright.sync_api import TimeoutError as BrowserTimeout
            try:
                apply_control.click()
            except BrowserTimeout:
                # A request may already have reached Naukri. Only read the result.
                if not re.search(r'application sent|applied successfully|application submitted|applied to ["\']', visible_page_text(page), re.I):
                    raise RuntimeError("Naukri submit timed out without a visible receipt; reconciliation required")
            page.wait_for_timeout(3000)
            missing_question = ""
            # Some Naukri direct applications ask sequential recruiter questions in a drawer.
            for _ in range(8):
                body = visible_page_text(page)
                if re.search(r"application sent|applied successfully|you have applied|application submitted|applied to [\"']", body, re.I):
                    break
                # Quota exhausted mid-flow: treat as SkippedBlocked, not ManualReview.
                if re.search(r"your daily quota has been expired|daily apply quota", body, re.I):
                    reason = "Naukri daily apply quota expired"
                    record_naukri_quota_exhausted()
                    safe_screenshot(page, screenshot)
                    db.execute("UPDATE jobs SET status='SkippedBlocked',eligibility_reason=? WHERE id=?", (reason, job["id"]))
                    db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=?", (reason, job["id"])); db.commit()
                    print(f"Skipped job {job['id']}: {reason}")
                    return
                question_locator = page.locator(".botMsg, .botItem, [class*='botMsg'], [class*='chatbot_ListItem'], .msg").last
                question_text = question_locator.inner_text().strip() if question_locator.count() else ""
                if not question_text:
                    dialog = page.locator(".chatbot_Drawer, ._chatBotContainer, [class*='chatBotContainer'], [class*='chatbot_Drawer'], [role='dialog'], aside").first
                    dialog_text = dialog.inner_text().strip() if dialog.count() else ""
                    question_text = dialog_text
                if not question_text:
                    break
                drawer_options = page.locator(".chatbot_Drawer label:not([class*='user']), .ssrc__label, [class*='chatbot'] [role='radio']").all_inner_texts()
                answer, _ = question_answer(question_text, options=[o.strip() for o in drawer_options if o.strip()], required=True)
                progressed = False
                if answer:
                    matched_choice = page.locator(".chatbot_Drawer label:not([class*='user']), .singleselect-radiobutton, label.ssrc__label, .ssrc__radio-btn-container, [class*='chatbot'] label:not([class*='user']), .chatbot_Drawer .botItem").filter(has_text=re.compile(rf"^\s*{re.escape(answer)}\s*$", re.I)).first
                    if not matched_choice.count():
                        matched_choice = page.locator(".chatbot_Drawer, aside, [role='dialog'], [class*='chatbot']").locator("label:not([class*='user']), .singleselect-radiobutton, [role='radio']").filter(has_text=re.compile(rf"\b{re.escape(answer)}\b", re.I)).first
                    if not matched_choice.count():
                        matched_choice = page.locator("label.ssrc__label, .ssrc__radio-btn-container, input[type='radio']").filter(has_text=re.compile(rf"{re.escape(answer)}", re.I)).first
                    if matched_choice.count():
                        try:
                            matched_choice.click(force=True)
                            page.wait_for_timeout(400)
                            progressed = True
                        except Exception:
                            pass
                if not progressed and answer:
                    text_input = page.locator(".chatbot_Drawer .textArea:visible, ._chatBotContainer .textArea:visible, [class*='chatbot'] .textArea:visible, .chatbot_Drawer [contenteditable='true']:visible, [class*='chatbot'] [contenteditable='true']:visible, .chatbot_Drawer input:not([type='radio']):not([type='checkbox']):not([type='button']):not([type='submit']):not([type='hidden']):visible, ._chatBotContainer input:not([type='radio']):not([type='checkbox']):not([type='button']):not([type='submit']):not([type='hidden']):visible, textarea:visible").last
                    if not text_input.count():
                        text_input = page.locator("input[placeholder*='message' i]:visible, textarea[placeholder*='message' i]:visible, div[data-placeholder*='message' i]:visible").last
                    if text_input.count():
                        fill_val = answer
                        if ('in lacs' in question_text.lower() or 'in years' in question_text.lower() or text_input.get_attribute('type') == 'number'):
                            num_m = re.search(r'\d+(?:\.\d+)?', answer)
                            if num_m:
                                fill_val = num_m.group(0)
                        text_input.click()
                        page.keyboard.press("Control+A")
                        page.keyboard.type(fill_val, delay=60)
                        try:
                            text_input.evaluate("""(el, val) => {
                                try { if (!el.innerText || el.innerText !== val) el.innerText = val; } catch(e){}
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            }""", fill_val)
                        except Exception:
                            pass
                        page.wait_for_timeout(350)
                        progressed = True
                if not progressed:
                    missing_question = question_text
                    db.execute("INSERT OR IGNORE INTO unknown_questions(normalized_question,question,job_id,company,created_at) VALUES(?,?,?,?,?)",
                               (norm(question_text), question_text, job["id"], job["company"], now()))
                    db.commit()
                    break
                save = page.locator(".chatbot_Drawer .send:not(.disabled) .sendMsg, .chatbot_Drawer .sendMsg, .chatbot_Drawer .send, .sendMsg, [class*='sendMsg'], .chatbot_Drawer div.send, [class*='chatbot'] .sendMsg, [class*='chatbot'] .send, .send:visible").first
                if not save.count():
                    save = page.locator(".chatbot_Drawer div:visible, ._chatBotContainer div:visible, [class*='chatbot'] div:visible").filter(has_text=re.compile(r"^\s*(save|submit|next|send)\s*$", re.I)).last
                if not save.count():
                    save = page.locator(".chatbot_Drawer button:visible, [class*='chatbot'] button:visible").filter(has_text=re.compile(r"^\s*(save|submit|next|send)\s*$", re.I)).last
                if not save.count(): break
                page.wait_for_timeout(600)
                save.click()
                page.wait_for_timeout(2500)
            body = visible_page_text(page)
            if re.search(r"application sent|applied successfully|you have applied|application submitted|applied to [\"']", body, re.I):
                safe_screenshot(page, screenshot)
                save_application_receipt(db, job, body, screenshot, page.url)
                db.execute("UPDATE jobs SET status='Applied',eligibility_reason='Naukri submission confirmed' WHERE id=?", (job["id"],))
                db.execute("UPDATE application_queue SET status='Applied',error=NULL WHERE job_id=?", (job["id"],)); db.commit()
                print(f"Applied: job {job['id']} — {job['company']} — {job['title']}")
                return
            reason = "Naukri Apply was not confirmed"
            if missing_question:
                reason = f"Required Naukri screening answer missing: {missing_question[:300]}"
            if page.url != before_url and "naukri.com" not in page.url.lower():
                reason = "Naukri redirected to an external application"
            elif blocking_challenge(page):
                reason = "Naukri CAPTCHA/challenge detected"
    safe_screenshot(page, screenshot)
    status = "ManualReview" if reason == "Naukri Apply was not confirmed" or reason.startswith("Required Naukri") else "SkippedBlocked"
    db.execute("UPDATE jobs SET status=?,eligibility_reason=? WHERE id=?", (status, reason, job["id"]))
    db.execute("UPDATE application_queue SET status=?,error=? WHERE job_id=?", ("ReviewRequired" if status == "ManualReview" else "Skipped", reason, job["id"])); db.commit()
    print(f"Skipped job {job['id']}: {reason}")


def mark_submission_started(db, job_id: int) -> None:
    check_stop()
    # Persist before clicking: a timeout or interruption may occur after the server accepts it.
    attempt = db.execute("SELECT id FROM application_attempts WHERE job_id=? AND completed_at IS NULL ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if not attempt:
        raise RuntimeError(f"No active application attempt exists for job {job_id}")
    # Atomic final-boundary check: pause marker, duplicate listings, concurrent claims.
    from application_state import claim_submission
    claim_submission(db, job_id, attempt['id'], DATA)
    from application_support import atomic_json
    directory = DATA / "screenshots" / f"job-{job_id}" / f"attempt-{attempt['id']}"
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "checkpoint.json", {
        "job_id":job_id,"attempt_id":attempt['id'],"step":"submit",
        "started_at":now(),"status":"Submission checkpoint saved before portal click",
    })


CONFIRMATION_PATTERN = re.compile(
    r"thank you|thanks for applying|application (?:submitted|received|complete|was sent)|"
    r"received your application|we've received your application|we will review your application|"
    r"successfully submitted|track your application|thanks for your interest", re.I)


def visible_submission_confirmation(page, surface, pattern=CONFIRMATION_PATTERN):
    """First visible confirmation element in the form surface, the page, or any embedded frame.

    Employer confirmations often render inside the Greenhouse/Ashby iframe, which a
    top-page text search never sees.
    """
    contexts, seen = [], set()
    try:
        frames = list(getattr(page, "frames", None) or [])
    except TypeError:
        frames = []
    for context in (surface, page, *frames):
        if context is not None and id(context) not in seen:
            seen.add(id(context)); contexts.append(context)
    for context in contexts:
        try:
            for item in context.get_by_text(pattern).all():
                try:
                    if item.is_visible():
                        return item
                except Exception:
                    continue
        except Exception:
            continue
    return None


def submit_control_still_visible(submit) -> bool:
    """A form that still shows its Submit button was not accepted."""
    try:
        return bool(submit.count() and submit.first.is_visible())
    except Exception:
        return False


_LIVENESS_CACHE: dict = {}


def posting_liveness(job, timeout: float = 10.0) -> tuple[str, str]:
    """Ask the ATS's public posting API whether a listing is still open.

    Returns ``("closed", reason)`` only on an authoritative 404/410 for the exact posting;
    anything uncertain (network error, unknown board, other ATS) is ``("unknown", "")`` so
    the browser attempt proceeds. This avoids spending minutes on removed postings whose
    company career pages still render a shell with no form.
    """
    ats = str(job["ats"] or "").lower()
    external_id = str(job["external_id"] or "").strip()
    if ats not in ("greenhouse", "lever") or not re.fullmatch(r"[0-9a-fA-F-]{6,}", external_id):
        return "unknown", ""
    try:
        companies = load_json("companies.json")
    except (OSError, ValueError):
        return "unknown", ""
    wanted = norm(job["company"])
    board = next((c.get("boardId") for c in companies if c.get("ats") == ats and norm(c.get("company", "")) == wanted), None)
    if not board:
        return "unknown", ""
    key = (ats, board, external_id)
    cached = _LIVENESS_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 600:
        return cached[1]
    url = (f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{external_id}" if ats == "greenhouse"
           else f"https://api.lever.co/v0/postings/{board}/{external_id}")
    result = ("unknown", "")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "JobPilot-local/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = ("open", "") if response.status == 200 else ("unknown", "")
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            result = ("closed", f"Job closed or removed: HTTP {exc.code} from {ats} posting API")
    except Exception:
        pass
    _LIVENESS_CACHE[key] = (time.monotonic(), result)
    return result


def cmd_apply(args: argparse.Namespace) -> None:
    with exclusive_lock(DATA / 'browser-application.lock'):
        _run_application_attempt(args)


def _run_application_attempt(args: argparse.Namespace) -> None:
    if not args.confirm_fill and not bool(getattr(args, "unattended", False)):
        raise SystemExit("Nothing was filled. Re-run with --confirm-fill to confirm transmitting your profile to the employer.")
    db = connect()
    try:
        recover_attempts(db, now())
        job = db.execute("SELECT * FROM jobs WHERE id=?", (args.job,)).fetchone() if args.job else db.execute("SELECT * FROM jobs WHERE status='Queued' ORDER BY score DESC LIMIT 1").fetchone()
        if not job:
            raise SystemExit("No matching queued job found.")
        if str(job["ats"]).casefold() == "linkedin":
            raise SystemExit("LinkedIn is a manual handoff. Open the listing in LinkedIn to apply.")
        if job["status"] == "Applied":
            raise SystemExit("This job is already marked Applied; another application was not opened.")
        if not getattr(args, 'dry_run', False):
            submitted = db.execute("SELECT 1 FROM application_attempts WHERE job_id=? AND step='submit' LIMIT 1", (job['id'],)).fetchone()
            if submitted:
                raise SystemExit('A submission checkpoint exists; review it before another application.')
            from application_state import assert_can_start, SubmissionBlocked
            try:
                assert_can_start(db, job['id'], DATA)
            except SubmissionBlocked as exc:
                raise SystemExit(str(exc))
            if getattr(args, 'unattended', False):
                settings = load_json('settings.json')
                if not settings.get('unattended') or not settings.get('autoSubmit'):
                    raise SystemExit('Unattended auto-submit is disabled in config/settings.json')
                if job['ats'] not in settings.get('allowedAutoSubmitAts', []):
                    raise SystemExit('This ATS is not enabled for unattended submission')
        args.job = job["id"]
        attempt = db.execute("INSERT INTO application_attempts(job_id,started_at,status,adapter,step) VALUES(?,?,'Running',?,'open')", (job["id"], now(), job["ats"])).lastrowid
        db.execute("UPDATE application_queue SET attempts=attempts+1 WHERE job_id=?", (job["id"],))
        db.commit()
        error = None
        import answer_engine as ae
        ae.set_context(job)
        try:
            _apply_job(args)
        except (Exception, SystemExit, KeyboardInterrupt) as exc:
            error = f"{type(exc).__name__}: {exc}"
            step = db.execute("SELECT step FROM application_attempts WHERE id=?", (attempt,)).fetchone()[0]
            status = "ManualReview" if step == "submit" else "FailedTemporary"
            db.execute("UPDATE jobs SET status=?,eligibility_reason=? WHERE id=?", (status, error, job["id"]))
            retry_at = None
            if step != "submit":
                queue = db.execute("SELECT attempts FROM application_queue WHERE job_id=?", (job["id"],)).fetchone()
                if queue:
                    retry_at = retry_time(load_json("settings.json"), queue["attempts"], now())
            db.execute("UPDATE application_queue SET status=?,error=?,next_attempt_at=? WHERE job_id=?",
                       ("ReviewRequired" if step == "submit" else status, error, retry_at, job["id"]))
            db.commit()
            raise
        finally:
            result = db.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
            if getattr(args, "dry_run", False) and error is None and result == "Queued": result = "DryRun"
            db.execute("UPDATE application_attempts SET completed_at=?,status=?,error=?,screenshot_path=? WHERE id=?", (now(), result, error, str(DATA / "screenshots" / f"job-{job['id']}-filled.png"), attempt))
            db.commit()
    finally:
        db.close()


def fill_known_fields(page, values):
    """Fill one pass; repeat when conditional questions appear."""
    for field in page.locator("input:visible:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox]):not([type=submit]):not([type=button]):not([role=combobox]):not([aria-hidden=true]), textarea:visible").all():
        check_stop()
        try:
            direct_meta = " ".join(filter(None, [field.get_attribute("name"), field.get_attribute("id"), field.get_attribute("placeholder"), field.get_attribute("aria-label")])).lower()
            field_id = field.get_attribute("id")
            if field_id:
                label = page.locator(f"label[for='{field_id}']")
                if label.count(): direct_meta += " " + label.first.inner_text().lower()
            surrounding = field.evaluate("""el => {
                const box = el.closest('.application-question, .posting-field, .custom-question') || el.closest('.application-field, .field, label, li, tr, div[class*="question"], div[class*="field"]');
                if (box && box.innerText) return box.innerText;
                if (el.id) {
                    const l = document.querySelector(`label[for="${el.id}"]`);
                    if (l && l.innerText) return l.innerText;
                }
                return el.parentElement ? el.parentElement.innerText : '';
            }""")
            meta = direct_meta + " " + str(surrounding).lower()
            match = next((values[key] for key in sorted(values, key=len, reverse=True) if key in norm(direct_meta)), None)
            if not match:
                tag = field.evaluate("el => el.tagName").lower()
                input_type = (field.get_attribute("type") or "").lower()
                kind = "textarea" if tag == "textarea" else "number" if input_type == "number" else "text"
                is_required = field.get_attribute("required") is not None or field.get_attribute("aria-required") == "true"
                match, _ = question_answer(meta, required=is_required or None, kind=kind)
            if match and not field.input_value():
                if any(loc_term in norm(meta) for loc_term in ("location", "city", "current location", "residence", "where are you based", "where are you located")):
                    if fill_typeahead_field(page, field, match, matching_option):
                        continue
                field.fill(match)
                field.evaluate("""(el, val) => {
                    try {
                        const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                        if (setter) setter.call(el, val);
                        else el.value = val;
                    } catch(e) { el.value = val; }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }""", match)
                if any(loc_term in norm(meta) for loc_term in ("location", "city")):
                    page.wait_for_timeout(800)
                    options = page.locator(".dropdown-results > *:visible, .location-option:visible, [class*='typeahead'] li:visible, [role='option']:visible")
                    if options.count():
                        candidates = options.all()
                        selected = matching_option(match, [option.inner_text() for option in candidates])
                        try:
                            if selected is not None: candidates[selected].click()
                        except Exception: pass
                    else:
                        selected_location = page.locator("#selected-location")
                        if selected_location.count() and not selected_location.input_value(): field.fill("")
        except Exception:
            continue
    for field in page.locator("select:visible").all():
        try:
            meta = " ".join(filter(None, [field.get_attribute("name"), field.get_attribute("id"), field.get_attribute("aria-label")])).lower()
            surrounding = field.evaluate("""el => {
                const box = el.closest('.application-question, .posting-field, .custom-question') || el.closest('.application-field, .field, label, li, tr, div[class*="question"], div[class*="field"]');
                return box ? box.innerText : el.parentElement ? el.parentElement.innerText : '';
            }""")
            options = field.locator("option").all()
            candidates = [o for o in options if o.get_attribute("value") and not o.is_disabled()]
            is_required = field.get_attribute("required") is not None or field.get_attribute("aria-required") == "true"
            answer, _ = question_answer(meta + " " + str(surrounding), options=[o.inner_text() for o in candidates],
                                        required=is_required or None, kind="choice")
            if answer and not field.input_value():
                selected = matching_option(answer, [o.inner_text() for o in candidates])
                if selected is not None: field.select_option(value=candidates[selected].get_attribute("value"))
        except Exception:
            continue
    # Greenhouse's newer forms render selects as buttons/listboxes instead of <select>.
    for trigger in page.locator("button:visible, [role='combobox']:visible").all():
        try:
            if "select" not in norm(trigger.inner_text()) and trigger.get_attribute("role") != "combobox":
                continue
            surrounding = trigger.evaluate("""el => {
                const labels = [...(el.labels || [])].map(l => l.innerText).join(' ');
                if (labels) return labels;
                const ids = (el.getAttribute('aria-labelledby') || '').split(/\\s+/);
                const labelled = ids.map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
                if (labelled) return labelled;
                const box = el.closest('.application-question, .field-wrapper, .select__container, .field');
                return box ? box.innerText : el.parentElement ? el.parentElement.innerText : '';
            }""")
            answer, _ = question_answer(str(surrounding))
            if not answer and any(k in norm(str(surrounding)) for k in ("location", "city", "residence")):
                answer = values.get("city") or values.get("location")
            if not answer: continue
            selected_value=trigger.evaluate("el => el.closest('.select__container, .field-wrapper, .field')?.querySelector('[class*=single-value], [class*=singleValue]')?.innerText || ''")
            if selected_value: continue
            trigger.click(); page.wait_for_timeout(250)
            if trigger.evaluate("el => el.tagName")=='INPUT' and not re.fullmatch(r'\d+(?:\.\d+)?',answer):
                trigger.evaluate("el => { try { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); } catch(e){} }")
                page.keyboard.type(answer, delay=45)
                page.wait_for_timeout(800)
            options = page.locator("[role='option']:visible, [role='listbox'] li:visible, [id*='option']:visible")
            try:
                options.first.wait_for(state='visible', timeout=2500)
            except Exception:
                pass
            candidates = options.all()
            selected = matching_option(answer, [o.inner_text() for o in candidates])
            if selected is not None: candidates[selected].click()
            else: page.keyboard.press("Escape")
        except Exception:
            continue
    # Ashby & React custom yes/no option buttons
    for yesno in page.locator(".ashby-application-form-input-yesno, [class*='input-yesno'], [class*='yesno']").all():
        try:
            surrounding = yesno.evaluate("""el => {
                const box = el.closest('.ashby-application-form-field-entry, .application-question, .posting-field, .custom-question') || el.closest('.application-field, .field, label, li, tr, div[class*="question"], div[class*="field"]');
                return box ? box.innerText : (el.parentElement ? el.parentElement.innerText : '');
            }""")
            answer, _ = question_answer(str(surrounding), options=["Yes", "No"], kind="choice")
            if answer:
                is_yes = str(answer).strip().lower() in ("yes", "y", "true", "1")
                opt = "yes" if is_yes else "no"
                btn = yesno.locator(f"button[data-option='{opt}'], button:has-text('{answer}')").first
                if btn and btn.count():
                    btn.click()
        except Exception:
            continue
    radio_names = page.locator("input[type=radio]:visible").evaluate_all("els => [...new Set(els.map(el => el.name).filter(Boolean))]")
    for radio_name in radio_names:
        try:
            radios = page.locator(f"input[type=radio][name='{radio_name}']:visible")
            if any(r.is_checked() for r in radios.all()): continue
            first = radios.first
            surrounding = first.evaluate("""el => {
                const box = el.closest('.application-question, .posting-field, .custom-question') || el.closest('.application-field, .field, label, li, tr, div[class*="question"], div[class*="field"]');
                return box ? box.innerText : el.parentElement ? el.parentElement.innerText : '';
            }""")
            radio_labels = []
            for radio in radios.all():
                radio_id = radio.get_attribute("id")
                radio_labels.append(page.locator(f"label[for='{radio_id}']").inner_text() if radio_id and page.locator(f"label[for='{radio_id}']").count() else radio.get_attribute("value") or "")
            answer, _ = question_answer(str(surrounding), options=[l for l in radio_labels if l.strip()], kind="choice")
            if answer:
                for radio in radios.all():
                    radio_id = radio.get_attribute("id")
                    label_text = page.locator(f"label[for='{radio_id}']").inner_text() if radio_id and page.locator(f"label[for='{radio_id}']").count() else radio.get_attribute("value") or ""
                    if norm(answer) == norm(label_text) or norm(answer) in norm(label_text):
                        radio.check(); break
        except Exception:
            continue
    for cb in page.locator("input[type=checkbox]:visible").all():
        try:
            if cb.is_checked():
                continue
            surrounding = cb.evaluate("""el => {
                const box = el.closest('.application-question, .posting-field, .custom-question, .field, label, li, tr, div[class*="question"], div[class*="field"], div[class*="checkbox"]');
                return box ? box.innerText : el.parentElement ? el.parentElement.innerText : '';
            }""")
            meta = " ".join(filter(None, [cb.get_attribute("name"), cb.get_attribute("id"), cb.get_attribute("aria-label"), str(surrounding)])).lower()
            is_required = cb.get_attribute("required") is not None or cb.get_attribute("aria-required") == "true"
            # Consents and attestations are ticked only under a standing consent the candidate granted.
            answer, _ = question_answer(meta, required=is_required or None, kind="checkbox")
            if answer and norm(str(answer)) in ("yes", "i agree", "agree", "true", "1", "i consent", "consent", "i acknowledge", "acknowledge"):
                try:
                    cb.check()
                except Exception:
                    try:
                        cb.click(force=True)
                    except Exception:
                        cb.evaluate("el => { el.checked = true; el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }")
        except Exception:
            continue


def dedupe_query_params(url: str) -> str:
    """Drop repeated identical query pairs (e.g. gh_jid=1&gh_jid=1), which loop some career sites."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(url or "")
    pairs = list(dict.fromkeys(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(parts._replace(query=urlencode(pairs)))


def _apply_job(args: argparse.Namespace) -> None:
    """Open one queued ATS form, fill safe known fields, and pause before submission."""
    unattended = bool(getattr(args, "unattended", False) or getattr(args, "dry_run", False))
    if not args.confirm_fill and not unattended:
        raise SystemExit("Nothing was filled. Re-run with --confirm-fill after checking the job and profile; this transmits your profile data to that employer.")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright is not installed. Run: python -m pip install -r requirements.txt")
    profile = load_json("profile.json")
    personal = profile["personal"]
    missing = [key for key in ("firstName", "lastName", "email", "phone") if not personal.get(key)]
    if missing:
        raise SystemExit(f"Complete config/profile.json first. Missing: {', '.join(missing)}")
    db = connect()
    if args.job:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (args.job,)).fetchone()
    else:
        job = db.execute("SELECT * FROM jobs WHERE status='Queued' ORDER BY score DESC LIMIT 1").fetchone()
    if not job:
        raise SystemExit("No matching queued job found.")
    live_state, live_reason = posting_liveness(job)
    if live_state == "closed":
        db.execute("UPDATE jobs SET status='Skipped',eligibility_reason=? WHERE id=?", (live_reason, job["id"]))
        db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=?", (live_reason, job["id"]))
        db.commit(); db.close()
        print(f"Skipped job {job['id']}: {live_reason}")
        return
    resume = DATA / "resumes" / (job["resume"] or "generic.pdf")
    pending_listing = job["source"] == "custom" and not job["company"]
    if not pending_listing and not resume.exists():
        raise SystemExit(f"Missing selected resume: {resume}")
    screenshot = DATA / "screenshots" / f"job-{job['id']}-filled.png"
    DATA.joinpath("screenshots").mkdir(parents=True, exist_ok=True)
    full_name = f"{personal['firstName']} {personal['lastName']}"
    current_company = profile["experience"].get("currentCompany")
    current_title = profile["experience"].get("currentTitle")
    values = {
        "full name": full_name, "name": full_name,
        "first name": personal["firstName"], "firstname": personal["firstName"],
        "last name": personal["lastName"], "lastname": personal["lastName"],
        "email": personal["email"], "phone": personal["phone"], "mobile": personal["phone"],
        "current location": personal.get("city"),
        "location": personal.get("city"), "city": personal.get("city"),
        "country": personal.get("country"),
        "linkedin": personal.get("linkedin", ""), "github": personal.get("github", ""),
        "portfolio": personal.get("portfolio", ""), "website": personal.get("portfolio", ""),
    }
    if current_company:
        values.update({"current company":current_company,"current employer":current_company})
    if current_title:
        values.update({"current title":current_title,"current designation":current_title})
    answers = load_json("answers.json")
    notice_period = answers.get("notice_period")
    if notice_period is None and profile["experience"].get("noticePeriodDays") is not None:
        notice_period = f"{profile['experience']['noticePeriodDays']} days"
    if notice_period:
        values.update({"notice period":str(notice_period),"how quickly can you join":str(notice_period)})
    for answer_key, fields in (("current_compensation",("current compensation","current ctc")),
                               ("expected_compensation",("expected compensation","expected ctc"))):
        if answers.get(answer_key):
            values.update({field:str(answers[answer_key]) for field in fields})
    with ExitStack() as cleanup:
        cleanup.callback(db.close)
        browser_profile = NAUKRI_PROFILE if job["ats"] == "naukri" else APPLICATION_PROFILE
        if browser_profile == NAUKRI_PROFILE:
            cleanup.enter_context(naukri_browser_lock(timeout=60.0))
        pw = cleanup.enter_context(sync_playwright())
        browser_profile.mkdir(parents=True, exist_ok=True)
        browser_args = []
        if unattended:
            browser_args.extend(["--window-position=-32000,-32000", "--window-size=1365,900", "--no-first-run"])
        context = pw.chromium.launch_persistent_context(
            str(browser_profile), channel="chrome", headless=unattended and job["ats"] != "naukri" and not getattr(args, "human_challenges", False),
            args=browser_args,
            viewport={"width": 1365, "height": 900})
        page = context.pages[0] if context.pages else context.new_page()
        cleanup.callback(context.close)
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        def save_trace():
            try: context.tracing.stop(path=str(DATA / "screenshots" / f"job-{job['id']}-trace.zip"))
            except Exception: pass
        cleanup.callback(save_trace)
        cleanup.callback(lambda: safe_screenshot(page, screenshot))
        page.set_default_timeout(10000)
        open_application(page,dedupe_query_params(job['apply_url']))
        page.wait_for_timeout(1500)
        body = visible_page_text(page)
        if pending_listing:
            try:
                company, title, description, location, posted = inspect_pasted_listing(page)
                if load_json("eligibility.json").get("maxPostingHours") and not posted:
                    raise ValueError("Original posting date not confirmed")
            except (ValueError, KeyError, AttributeError, TypeError) as exc:
                reason = f"Pasted listing needs review: {exc}"
                if blocking_challenge(page):
                    reason = "Verification challenge on pasted listing"
                elif re.search(r"login to apply|sign in to apply", body, re.I):
                    reason = "Login required on pasted listing"
                db.execute("UPDATE jobs SET status='ManualReview',eligibility_reason=? WHERE id=?", (reason,job["id"]))
                db.execute("UPDATE application_queue SET status='ReviewRequired',error=? WHERE job_id=?", (reason,job["id"]))
                db.commit()
                return
            selected = application_resume_for(title+" "+description, job["id"])
            db.execute("UPDATE jobs SET company=?,title=?,description=?,location=?,posted_at=?,resume=? WHERE id=?",
                       (company,title,description,location,posted,selected,job["id"]))
            db.commit()
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            resume = DATA / "resumes" / selected
            if not resume.is_file():
                raise ValueError(f"Missing selected resume: {selected}")
        if posting_closed(page, job['apply_url']) or re.search(r"job posting you're looking for|no longer available|this job has expired|this position has been filled|couldn't find anything here|job has closed", body, re.I):
            safe_screenshot(page, screenshot)
            db.execute("UPDATE jobs SET status='Skipped',eligibility_reason='Job posting expired or no longer available' WHERE id=?", (job["id"],))
            db.execute("UPDATE application_queue SET status='Skipped',error='Job posting expired or no longer available' WHERE job_id=?", (job["id"],))
            db.commit()
            print(f"Skipped job {job['id']}: Job posting expired or no longer available")
            return
        if unattended and not getattr(args, 'dry_run', False):
            eligibility_job = dict(job)
            if job['ats'] == 'naukri':
                page_metadata = naukri_page_metadata(page)
                if page_metadata:
                    # Treat the live listing metadata as authoritative over
                    # incomplete or stale text imported from a search card.
                    metadata_job = dict(job)
                    metadata_job['description'] = page_metadata
                    metadata_ok, metadata_reason = eligible(metadata_job, load_json('eligibility.json'))
                    if not metadata_ok:
                        db.execute("UPDATE jobs SET status='Skipped',eligibility_reason=? WHERE id=?", (metadata_reason,job['id']))
                        db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=?", (metadata_reason,job['id']))
                        db.commit()
                        safe_screenshot(page, screenshot)
                        print(f"Skipped job {job['id']}: {metadata_reason}")
                        return
                    eligibility_job['description'] = "\n".join(filter(None, (job['description'], page_metadata)))
            ok, reason = eligible(eligibility_job, load_json('eligibility.json'))
            if pending_listing:
                db.execute("UPDATE jobs SET eligible=? WHERE id=?", (ok,job["id"]))
                db.commit()
            if not ok:
                db.execute("UPDATE jobs SET status='Skipped',eligibility_reason=? WHERE id=?", (reason,job['id']))
                db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=?", (reason,job['id']))
                db.commit()
                print(f"Skipped job {job['id']}: {reason}")
                return
        if not wait_for_manual_challenge(page, unattended and not getattr(args, "human_challenges", False), company=job["company"], job_id=job["id"]):
            safe_screenshot(page, screenshot)
            status = "SkippedBlocked" if unattended else "ManualReview"
            db.execute("UPDATE jobs SET status=?, eligibility_reason='CAPTCHA detected' WHERE id=?", (status, job["id"]));
            db.execute("UPDATE application_queue SET status='Skipped',error='CAPTCHA detected' WHERE job_id=?", (job["id"],)); db.commit()
            if not unattended: input("CAPTCHA/challenge detected. Job moved to Manual Review. Press Enter to close.")
            db.close(); return
        if job["ats"] == "naukri" and unattended and not getattr(args, "dry_run", False):
            external = page.locator("button:visible, a:visible, [role=button]:visible").filter(
                has_text=re.compile(r"^\s*apply on (?:the )?company (?:web)?site\s*$", re.I)).first
            # Prefer Naukri's own signed-in direct-apply flow over external redirects.
            # Only follow "Apply on company site" when no Naukri direct-apply control is present.
            direct_control = naukri_direct_apply_control(page)
            if not external.count() or direct_control or re.search(r"login to apply|register to apply|sign in to apply", body, re.I):
                try_naukri_direct_apply(page, db, job, screenshot)
                return
            original_url = page.url
            prior_pages = set(context.pages)
            external.click()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                popups = [opened for opened in context.pages if opened not in prior_pages]
                destination = popups[-1] if popups else page
                if destination.url not in ("about:blank", original_url) and ats_for(destination.url) != "naukri":
                    break
                page.wait_for_timeout(200)
            page = destination
            page.wait_for_load_state("domcontentloaded")
            adapter = ats_for(page.url)
            if adapter == "generic":
                from ats_detector import detect_page
                adapter, _ = detect_page(page, urlparse(page.url).hostname or "unknown")
            from ats_forms import supports_autosubmit
            reason = None
            if blocking_challenge(page):
                reason = "Verification challenge on Naukri Apply destination"
            elif re.search(r"(?:sign|log) in to apply|login to apply", visible_page_text(page), re.I):
                reason = "Login required on Naukri Apply destination"
            elif (urlparse(page.url).scheme != "https" or adapter == "naukri"
                  or not supports_autosubmit(adapter) or adapter not in load_json("settings.json")["allowedAutoSubmitAts"]):
                reason = f"Unsupported Naukri Apply destination: {urlparse(page.url).hostname} ({adapter})"
            if reason:
                db.execute("UPDATE jobs SET status='SkippedBlocked',eligibility_reason=? WHERE id=?", (reason, job["id"]))
                db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=?", (reason, job["id"]))
                db.commit()
                print(reason)
                return
            # Keep the board as the source and retain the resolved requisition for
            # duplicate checks across pasted links, discovery and worker restarts.
            existing = db.execute("SELECT id FROM jobs WHERE apply_url=? AND id<>?", (page.url, job["id"])).fetchone()
            if existing:
                reason = f"Employer form already tracked by job {existing['id']}"
                db.execute("UPDATE jobs SET status='ManualReview',eligibility_reason=? WHERE id=?", (reason, job["id"]))
                db.execute("UPDATE application_queue SET status='ReviewRequired',error=? WHERE job_id=?", (reason, job["id"]))
                db.commit()
                return
            db.execute("UPDATE jobs SET apply_url=?,ats=? WHERE id=?", (page.url, adapter, job["id"]))
            db.commit()
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
        # Upload first: ATS resume parsing may populate or overwrite contact fields.
        # JobPilot's verified profile values are applied afterward.
        surface = prepare_application(page, job["ats"])
        if not wait_for_manual_challenge(surface, unattended and not getattr(args, "human_challenges", False), company=job["company"], job_id=job["id"]):
            safe_screenshot(page, screenshot)
            db.execute("UPDATE jobs SET status='SkippedBlocked',eligibility_reason='CAPTCHA detected in application frame' WHERE id=?", (job["id"],))
            db.execute("UPDATE application_queue SET status='Skipped',error='CAPTCHA detected in application frame' WHERE job_id=?", (job["id"],)); db.commit()
            return
        files = surface.locator(ADAPTERS.get(job["ats"], GENERIC).resume)
        if not files.count():
            files = surface.locator("input[type=file]:not([name*=cover i]):not([id*=cover i])")
        if files.count():
            files.first.set_input_files(str(resume))
            page.wait_for_timeout(3000)
        scroll_all_cards(surface)
        seen_steps = set()
        for step in range(5):
            previous = None
            for _ in range(3):
                scroll_all_cards(surface)
                fill_known_fields(surface, values)
                fields = inspect_fields(surface)
                signature = [(f["name"], f["missing"], f["invalid"]) for f in fields]
                if signature == previous: break
                previous = signature
                page.wait_for_timeout(300)
            if unresolved_fields(surface): break
            navigation = next_application_control(surface)
            fingerprint = tuple((f['name'], f['type'], f['question']) for f in fields)
            if navigation is None or fingerprint in seen_steps or step == 4: break
            seen_steps.add(fingerprint)
            navigation.click()
            page.wait_for_timeout(700)
        scroll_all_cards(surface)
        unresolved = collect_question_options(surface, unresolved_fields(surface))
        report = DATA / "screenshots" / f"job-{job['id']}-questions.json"
        report.write_text(json.dumps(unresolved, indent=2, ensure_ascii=False), encoding="utf-8")
        for field in unresolved:
            db.execute("INSERT OR IGNORE INTO unknown_questions(normalized_question,question,job_id,company,created_at) VALUES(?,?,?,?,?)",
                       (norm(field["question"]), field["question"], job["id"], job["company"], now()))
        db.commit()
        if getattr(args, "dry_run", False):
            safe_screenshot(page, screenshot)
            print(f"Fill-only job {job['id']}: {len(unresolved)} unresolved fields. Report: {report}")
            db.execute("UPDATE application_queue SET mode='DRY_RUN' WHERE job_id=?", (job["id"],)); db.commit()
            return
        if unattended:
            challenge_blocked = blocking_challenge(surface) or blocking_challenge(page)
            rendered_text = visible_page_text(surface)
            account_blocked = bool(re.search(
                r"(?:enter|send|required|provide).{0,30}(?:verification code|one.time password)|(?:sign|log) in to apply",
                rendered_text, re.I))
            missing = [f["question"] for f in unresolved]
            if challenge_blocked or account_blocked or missing:
                reason = "CAPTCHA/OTP/login detected" if challenge_blocked or account_blocked else "Missing required fields: " + ", ".join(missing[:8])
                if missing:
                    import answer_engine as ae
                    needs = sorted({r.needs for _, r in ae.blockers() if r.needs})
                    if needs:
                        reason += " | Needs: " + "; ".join(needs[:6])
                safe_screenshot(page, screenshot)
                db.execute("UPDATE jobs SET status='SkippedBlocked',eligibility_reason=? WHERE id=?", (reason, job["id"]));
                db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=?", (reason, job["id"])); db.commit()
                print(f"Skipped job {job['id']}: {reason}")
                db.close(); return
            settings = load_json("settings.json")
            if job["ats"] not in settings.get("allowedAutoSubmitAts", []):
                reason = f"ATS not enabled for auto-submit: {job['ats']}"
                db.execute("UPDATE jobs SET status='SkippedBlocked',eligibility_reason=? WHERE id=?", (reason, job["id"]));
                db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=?", (reason, job["id"])); db.commit()
                print(f"Skipped job {job['id']}: {reason}"); db.close(); return
            submit = submission_control(surface, job["ats"])
            if submit is None:
                reason = "No supported submit control"
                db.execute("UPDATE jobs SET status='SkippedBlocked',eligibility_reason=? WHERE id=?", (reason, job["id"]));
                db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=?", (reason, job["id"])); db.commit()
                print(f"Skipped job {job['id']}: {reason}"); db.close(); return
            mark_submission_started(db, job["id"])
            from playwright.sync_api import TimeoutError as BrowserTimeout
            try:
                submit.click(no_wait_after=True)
            except BrowserTimeout:
                print("Submit timed out; checking this page for confirmation before any retry.", flush=True)
            page.wait_for_timeout(1500)
            if blocking_challenge(page):
                if not wait_for_manual_challenge(page, unattended and not getattr(args, "human_challenges", False), company=job["company"], job_id=job["id"]):
                    safe_screenshot(page, screenshot)
                    db.execute("UPDATE jobs SET status='SkippedBlocked',eligibility_reason='CAPTCHA detected on submit' WHERE id=?", (job["id"],))
                    db.execute("UPDATE application_queue SET status='Skipped',error='CAPTCHA detected on submit' WHERE job_id=?", (job["id"],)); db.commit()
                    print(f"Skipped job {job['id']}: CAPTCHA detected on submit"); db.close(); return
            if email_verification_inputs(page, surface)[1] is not None:
                reason = "Verification code required after submission; reconcile before retry"
                db.execute("UPDATE jobs SET status='ManualReview',eligibility_reason=? WHERE id=?", (reason, job["id"]))
                db.execute("UPDATE application_queue SET status='ReviewRequired',error=? WHERE job_id=?", (reason, job["id"]))
                db.commit()
                return
            conf_regex = CONFIRMATION_PATTERN
            deadline = time.monotonic() + 12
            confirmation_item = None
            while time.monotonic() < deadline:
                confirmation_item = visible_submission_confirmation(page, surface, conf_regex)
                if confirmation_item is not None:
                    break
                page.wait_for_timeout(500)
            page_text = visible_page_text(page)
            form_still_open = submit_control_still_visible(submit)
            url_confirmed = any(tok in page.url.lower() for tok in ['/confirmation', '/submitted', '/success', '/thank-you', '/complete'])
            # Text alone is not proof while the Submit button is still on screen.
            text_confirmed = (confirmation_item is not None or bool(conf_regex.search(page_text))) and not form_still_open
            url_confirmed = url_confirmed and not form_still_open

            if text_confirmed or url_confirmed:
                try:
                    confirmation_text = confirmation_item.inner_text() if confirmation_item is not None else None
                except Exception:
                    confirmation_text = None
                if not confirmation_text:
                    m = conf_regex.search(page_text)
                    confirmation_text = m.group(0) if m else "Submission confirmed"
                safe_screenshot(page, screenshot)
                save_application_receipt(db, job, confirmation_text, screenshot, page.url)
                db.execute("UPDATE jobs SET status='Applied',eligibility_reason='Submission confirmed' WHERE id=?", (job["id"],));
                db.execute("UPDATE application_queue SET status='Applied',error=NULL WHERE job_id=?", (job["id"],)); db.commit()
                print(f"Applied: job {job['id']} - {job['company']} - {job['title']}")
            elif re.search(r"verification code|one.time password|confirm (?:that )?you.re a human", visible_page_text(page), re.I):
                safe_screenshot(page, screenshot)
                reason = "Email/SMS verification required after submit"
                db.execute("UPDATE jobs SET status='SkippedBlocked',eligibility_reason=? WHERE id=?", (reason, job["id"]));
                db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=?", (reason, job["id"])); db.commit()
                print(f"Skipped job {job['id']}: {reason}")
            else:
                safe_screenshot(page, screenshot)
                reason = "CAPTCHA after Submit; human verification required" if blocking_challenge(page) or blocking_challenge(surface) else "Submission confirmation not detected"
                db.execute("UPDATE jobs SET status='ManualReview',eligibility_reason=? WHERE id=?", (reason, job["id"]));
                db.execute("UPDATE application_queue SET status='ReviewRequired',error=? WHERE job_id=?", (reason, job["id"])); db.commit()
                print(f"Unconfirmed job {job['id']}: {reason}")
            db.close(); return
        safe_screenshot(page, screenshot)
        db.execute("UPDATE application_queue SET mode='SEMI_AUTO',status='ReviewRequired' WHERE job_id=?", (job["id"],)); db.commit()
        print(f"Filled job {job['id']}: {job['company']} — {job['title']}\nScreenshot: {screenshot}\nSubmit was NOT clicked.")
        input("Review the open application. Submit it yourself if everything is correct, then press Enter to close CareerOS.")
    db.close()


def cmd_apply_batch(args: argparse.Namespace) -> None:
    with exclusive_lock(DATA / 'agent-worker.lock'):
        with exclusive_lock(DATA / 'application-batch.lock'):
            _run_application_batch(args)


def _run_application_batch(args: argparse.Namespace) -> None:
    settings = load_json("settings.json")
    if not getattr(args, "dry_run", False) and (not settings.get("unattended") or not settings.get("autoSubmit")):
        raise SystemExit("Unattended auto-submit is disabled in config/settings.json")
    from ats_forms import supports_autosubmit
    allowed = [str(x).lower() for x in settings.get("allowedAutoSubmitAts", [])]
    if not allowed:
        raise SystemExit("No ATS adapters are enabled in allowedAutoSubmitAts")
    dry_run = getattr(args, "dry_run", False)
    rules = load_json("eligibility.json")
    with closing(connect()) as db:
        candidates = db.execute("""SELECT j.* FROM application_queue q JOIN jobs j ON j.id=q.job_id
            WHERE q.status='Queued' AND j.status='Queued' ORDER BY q.score DESC,j.id""").fetchall()
        ids = []
        rejected = []
        for job in candidates:
            ats = str(job["ats"]).lower()
            if ats not in allowed or (not dry_run and not supports_autosubmit(ats)):
                if not dry_run:
                    rejected.append((f"Unsupported submission adapter: {ats}", job["id"]))
                continue
            if not dry_run:
                ok, reason = eligible(job, rules)
                if not ok and not (job["source"] == "custom" and not job["company"]):
                    rejected.append((reason, job["id"]))
                    continue
            if len(ids) < args.limit:
                ids.append(job["id"])
        with db:
            for reason, job_id in rejected:
                changed = db.execute("""UPDATE jobs SET status='Skipped',eligibility_reason=?
                    WHERE id=? AND status='Queued' AND NOT EXISTS (
                        SELECT 1 FROM application_attempts a WHERE a.job_id=jobs.id
                        AND (a.completed_at IS NULL OR a.step='submit'))""", (reason, job_id))
                if changed.rowcount:
                    db.execute("""UPDATE application_queue SET status='Skipped',error=?
                        WHERE job_id=? AND status='Queued'""", (reason, job_id))
    print(f"Unattended batch selected: {len(ids)} (ATS: {', '.join(allowed)})")
    try:
        for position, job_id in enumerate(ids, start=1):
            details = connect()
            selected = details.execute("SELECT company,title FROM jobs WHERE id=?", (job_id,)).fetchone()
            details.close()
            print(f"[{position}/{len(ids)}] Starting job {job_id}: {selected['company']} — {selected['title']}", flush=True)
            try:
                cmd_apply(argparse.Namespace(job=job_id, confirm_fill=True, unattended=True, dry_run=getattr(args, "dry_run", False), human_challenges=getattr(args, "human_challenges", False)))
            except (Exception, SystemExit) as exc:
                print(f"Failed job {job_id}: {exc}")
    except KeyboardInterrupt:
        print("\nBatch stopped by user. Remaining queued jobs were not changed.")


def cmd_retry(args: argparse.Namespace) -> None:
    db = connect()
    from application_support import retry_policy
    max_attempts, _, _ = retry_policy(load_json("settings.json"))
    # Clear stale Queued rows that already hit attempt limit so the live queue never stalls.
    exhausted = db.execute(
        "SELECT q.job_id FROM application_queue q JOIN jobs j ON j.id=q.job_id WHERE q.status='Queued' AND j.status='Queued' AND q.attempts>=?",
        (max_attempts,)
    ).fetchall()
    for row in exhausted:
        reason = "Application attempt limit reached; review saved evidence"
        db.execute("UPDATE jobs SET status='ManualReview',eligibility_reason=? WHERE id=?", (reason, row["job_id"]))
        db.execute("UPDATE application_queue SET status='ReviewRequired',error=? WHERE job_id=?", (reason, row["job_id"]))
    cond = """(
        status='FailedTemporary'
        OR (status='ManualReview' AND ats='naukri' AND eligibility_reason='Naukri Apply was not confirmed')
        OR (status='ManualReview' AND eligibility_reason LIKE 'TimeoutError:%')
        OR (status='SkippedBlocked' AND (eligibility_reason LIKE 'Missing required fields:%'))
        OR (status='SkippedBlocked' AND eligibility_reason='Naukri daily apply quota expired')
    )"""
    rows = db.execute(f"""SELECT * FROM jobs WHERE ({cond})
        AND NOT EXISTS (SELECT 1 FROM application_attempts a WHERE a.job_id=jobs.id AND (a.status='Applied' OR a.step='submit' OR a.completed_at IS NULL))
        AND status != 'Applied'""").fetchall()
    requeued = 0
    quota_paused = naukri_quota_active()
    from application_state import submitted_duplicates
    timestamp = now()
    for row in rows:
        queue = db.execute("SELECT attempts,next_attempt_at FROM application_queue WHERE job_id=?", (row["id"],)).fetchone()
        if queue and (queue["attempts"] >= max_attempts or not due(queue["next_attempt_at"], timestamp)):
            continue
        if quota_paused and row["ats"] == "naukri" and str(row["eligibility_reason"] or "") == "Naukri daily apply quota expired":
            continue
        # A submit checkpoint means the employer may have the application already.
        duplicates = submitted_duplicates(db, row)
        if duplicates:
            reason = 'Another listing of this opening was already submitted: job ' + ', '.join(map(str, duplicates))
            db.execute("UPDATE jobs SET status='SkippedBlocked',eligibility_reason=? WHERE id=?", (reason, row['id']))
            db.execute("UPDATE application_queue SET status='SkippedBlocked',error=? WHERE job_id=?", (reason, row['id']))
            continue
        # Missing answers are resolved only by new facts or consents, never inferred here.
        reason_str = str(row["eligibility_reason"] or "")
        if "Missing required fields:" in reason_str and not blockers_resolved(row["id"]):
            continue

        ok, reason = eligible(row, load_json("eligibility.json"))
        if not ok:
            db.execute("UPDATE jobs SET status='Skipped',eligibility_reason=? WHERE id=?", (reason, row['id']))
            db.execute("UPDATE application_queue SET status='Skipped',error=? WHERE job_id=?", (reason, row['id']))
            continue
        requeued += 1
        db.execute("UPDATE jobs SET status='Queued',eligibility_reason='Retry queued after mapper/answer update' WHERE id=?", (row['id'],))
        db.execute("""INSERT INTO application_queue(job_id, priority, score, status, created_at)
                      VALUES(?, ?, ?, 'Queued', ?)
                      ON CONFLICT(job_id) DO UPDATE SET status='Queued',error=NULL""",
                   (row['id'], row['priority'] or 'MEDIUM', row['score'] or 1.0, now()))
    db.commit(); db.close(); print(f"Requeued fixable applications: {requeued}")


def cmd_run(args: argparse.Namespace) -> None:
    cmd_discover(args); cmd_score(args); cmd_queue(args); cmd_export(args); cmd_status(args)
    if getattr(args, "apply", False):
        cmd_apply_batch(argparse.Namespace(limit=args.limit or load_json("settings.json")["dailyTarget"]))
        cmd_export(args)


def cmd_doctor(_: argparse.Namespace) -> None:
    """Check local setup without visiting employers."""
    problems = []
    for name in ("profile.json", "settings.json", "answers.json", "question-bank.json", "companies.json", "eligibility.json", "skills.json"):
        try: load_json(name)
        except (OSError, ValueError) as exc: problems.append(f"{name}: {exc}")
    try:
        personal = load_json("profile.json")["personal"]
        for key in ("firstName", "lastName", "email", "phone"):
            if not str(personal.get(key, "")).strip(): problems.append(f"Missing profile field: {key}")
    except (OSError, ValueError, KeyError): problems.append("Invalid profile structure")
    for name in ("backend.pdf", "fullstack.pdf", "platform-sre.pdf", "security.pdf", "generic.pdf"):
        path = DATA / "resumes" / name
        if not path.is_file() or path.stat().st_size == 0: problems.append(f"Missing/empty resume: {name}")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True, timeout=15000)
            try:
                page = browser.new_page()
                page.set_content("<label>Name<input required></label>")
                page.locator("input").fill("Local setup check")
                if page.locator("input").input_value() != "Local setup check": problems.append("Browser form check failed")
            finally: browser.close()
    except Exception as exc: problems.append(f"Chrome/Playwright startup failed: {exc}")
    for problem in problems: print(f"Needs attention: {problem}")
    if problems: raise SystemExit(1)
    print("Local setup passed: configuration, contacts, resumes, Chrome and form filling. Live portal sessions require separate verification.")


py = sys.executable
def recover_application_attempts(db, timestamp=None):
    return recover_attempts(db, timestamp or now())


def application_resume_for(text: str, job_id: int | None = None) -> str:
    profile = load_json("profile.json")
    resumes = profile.get("resumes", {})
    default = resumes.get("default", "generic.pdf")
    t = (text or "").lower()
    for category, filename in resumes.items():
        if category in ("default", "generic"):
            continue
        if category in t or (category == "fullstack" and any(k in t for k in ("fullstack", "full stack", "full-stack"))):
            return filename
        if category == "backend" and any(k in t for k in ("backend", "back end", "back-end", ".net", "c#", "python")):
            return filename
    return default


def candidate_config_revision() -> str:
    h = hashlib.sha256()
    for fn in ("profile.json", "answers.json", "eligibility.json", "settings.json"):
        p = CONFIG / fn
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def write_candidate_config(profile, answers, revision=None) -> str:
    (CONFIG / "profile.json").write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (CONFIG / "answers.json").write_text(json.dumps(answers, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _JSON_CACHE.pop("profile.json", None)
    _JSON_CACHE.pop("answers.json", None)
    return candidate_config_revision()


def blockers_resolved(job_id: int) -> bool:
    """True when every recorded answer blocker for the job now resolves."""
    import answer_engine as ae
    db = candidate_db()
    try:
        rows = db.execute("SELECT question FROM answer_blockers WHERE job_id=? AND resolved_at IS NULL", (job_id,)).fetchall()
        if not rows:
            return False
        jobs = connect()
        try:
            job = jobs.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            jobs.close()
        ctx = ae.context_for(job) if job else ae.Context(job_id=job_id)
        open_ = [r["question"] for r in rows if ae.resolve(ae.Field(r["question"], required=True), ctx, db).action != ae.FILL]
        if not open_:
            db.execute("UPDATE answer_blockers SET resolved_at=? WHERE job_id=? AND resolved_at IS NULL", (now(), job_id)); db.commit()
        return not open_
    finally:
        db.close()


def promote_due_retries(db: sqlite3.Connection) -> int:
    current = now()
    maximum, _, _ = retry_policy(load_json("settings.json"))
    c = db.execute("""
        UPDATE application_queue
        SET status = 'Queued'
        WHERE status = 'FailedTemporary' AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?
          AND attempts < ?
          AND EXISTS (SELECT 1 FROM jobs j WHERE j.id=application_queue.job_id AND j.status='FailedTemporary')
          AND NOT EXISTS (SELECT 1 FROM application_attempts a WHERE a.job_id=application_queue.job_id
                          AND (a.step='submit' OR a.status='Applied' OR a.completed_at IS NULL))
    """, (current, maximum))
    count = c.rowcount
    if count:
        db.execute("""
            UPDATE jobs
            SET status = 'Queued'
            WHERE id IN (
                SELECT job_id FROM application_queue WHERE status = 'Queued'
            ) AND status = 'FailedTemporary'
        """)
        db.commit()
    return count


def import_custom_job(url: str):
    """Durably enqueue a pasted listing, even while the browser is busy."""
    from application_state import canonical_url
    from ats_forms import supports_autosubmit
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Provide a public HTTPS job listing URL")
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        fp = hashlib.sha256(canonical_url(url).encode()).hexdigest()
        row = db.execute("SELECT * FROM jobs WHERE source_url=? OR apply_url=? OR fingerprint=? ORDER BY id LIMIT 1", (url,url,fp)).fetchone()
        if not row:
            db.execute("""INSERT INTO jobs(company,title,source,source_url,apply_url,ats,discovered_at,fingerprint)
                       VALUES('','Listing pending inspection','custom',?,?,?,?,?)""", (url,url,ats_for(url),now(),fp))
            row = db.execute("SELECT * FROM jobs WHERE fingerprint=?", (fp,)).fetchone()
        job_id = row["id"]
        prior = db.execute("SELECT 1 FROM application_attempts WHERE job_id=? AND (step='submit' OR completed_at IS NULL)", (job_id,)).fetchone()
        if prior or row["status"] in ("Applied","Rejected","ManualReview","SkippedBlocked","FailedTemporary"):
            return job_id, row["status"], row["eligibility_reason"]
        reason = "Pasted link queued for eligibility and screening checks"
        status = "Queued"
        if not supports_autosubmit(row["ats"]):
            status, reason = "ManualReview", f"Unsupported application form: {row['ats']}"
        elif row["company"]:
            ok, detail = eligible(row, load_json("eligibility.json"))
            if not ok:
                status, reason = "Skipped", detail
        db.execute("UPDATE jobs SET status=?,eligibility_reason=? WHERE id=?", (status,reason,job_id))
        db.execute("""INSERT INTO application_queue(job_id,priority,score,mode,status,error,created_at)
                   VALUES(?,?,?,'USER_LINK',?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                   mode='USER_LINK',status=excluded.status,error=excluded.error""",
                   (job_id,row["priority"],row["score"],status,None if status=="Queued" else reason,now()))
        db.commit()
        return job_id,status,reason
    finally:
        db.close()


def inspect_pasted_listing(page):
    """Read facts about this listing only, before any profile data is entered."""
    postings = page.locator('script[type="application/ld+json"]').evaluate_all("""scripts => {
      const jobs = [];
      function visit(item) {
        if (Array.isArray(item)) item.forEach(visit);
        else if (item && typeof item === 'object') {
          if ([].concat(item['@type']).includes('JobPosting')) jobs.push(item);
          if (item['@graph']) visit(item['@graph']);
        }
      }
      for (const script of scripts) { try { visit(JSON.parse(script.textContent)); } catch {} }
      return jobs;
    }""")
    if len(postings) != 1:
        raise ValueError("Cannot verify a unique JobPosting on the pasted listing")
    item = postings[0]
    locations = item.get("jobLocation") or []
    locations = locations if isinstance(locations,list) else [locations]
    def location(place):
        address = place["address"]
        country = address.get("addressCountry") or ""
        country = country.get("name") if isinstance(country,dict) else country
        return ", ".join(filter(None, (address.get("addressLocality"), address.get("addressRegion"),
                                      "India" if country == "IN" else country)))
    values = (item["hiringOrganization"]["name"],item["title"],plain_html(item["description"]),
              "; ".join(location(place) for place in locations),item.get("datePosted") or "")
    if not all(values[:4]):
        raise ValueError("Employer, role, description or location missing on the pasted listing")
    return values


def queue_candidates(db: sqlite3.Connection, limit: int = 10):
    return db.execute("""
        SELECT j.id, j.company, j.title, j.ats, q.score, q.priority, q.status, q.attempts, q.next_attempt_at
        FROM application_queue q
        JOIN jobs j ON j.id = q.job_id
        WHERE q.status = 'Queued' AND j.status = 'Queued'
        ORDER BY q.score DESC, j.id ASC
        LIMIT ?
    """, (limit,)).fetchall()


def cmd_remember_answer(args):
    """Save the candidate's own answer to one exact question (user-confirmed)."""
    import answer_engine as ae, candidate_kb as kb
    db = candidate_db()
    try:
        kb.put_fact(db, 'answer.question:' + ae.norm(args.question)[:200], args.answer, source='candidate',
                    verification=kb.USER_CONFIRMED)
    finally:
        db.close()
    print('Answer saved for this exact question; it is reused only where the wording matches.')


CONSENT_KINDS = ('privacy_notice', 'truthful_certification', 'background_check', 'demographic_processing', 'sms', 'recording', 'terms', 'marketing')


def cmd_facts(args):
    """Review and confirm candidate facts used for screening answers."""
    import answer_engine as ae, candidate_kb as kb
    db = candidate_db()
    try:
        action = args.facts_action
        if action == 'import':
            print('Imported (versions are only added when a value changed).')
        elif action == 'review':
            live = db.execute("""SELECT key,scope,value,source,verification FROM facts f
                WHERE verification IN ('user_confirmed','user_saved','resume','derived')
                AND version=(SELECT MAX(version) FROM facts g WHERE g.key=f.key AND g.scope=f.scope)
                AND key NOT LIKE 'meta.%' ORDER BY key""").fetchall()
            print(f"Live facts ({len(live)}):")
            for r in live:
                value = r['value'] if len(r['value']) < 80 else r['value'][:77] + '...'
                print(f"  {r['key']}@{r['scope']} = {value}  [{r['verification']}; {r['source']}]")
            print('Employment:', ', '.join(f"{e['employer']} {e['start']} to {e['end'] or 'present'} ({e['type']})" for e in kb.employment(db)))
            years, _ = kb.total_experience(db)
            print(f'Derived full-time experience: {years:.2f} years')
            usage = db.execute('SELECT skill,context,start,end FROM skill_usage ORDER BY skill').fetchall()
            print('Confirmed skill usage:', ', '.join(f"{u['skill']} {u['start']} to {u['end'] or 'present'}" for u in usage)
                  or 'none (numeric skill-years questions will block)')
            granted = [k for k in CONSENT_KINDS if (f := kb.get_fact(db, 'consent.' + k)) and ae._bool(f.value)]
            print('Standing consents:', ', '.join(granted) or 'none (consent and attestation fields will block)')
            for c in kb.conflicts(db):
                print(f"CONFLICT {c['key']}@{c['scope']}: {c['values']}")
            print(f"Legacy unverified values kept out of live answers: {len(kb.legacy_facts(db))}")
            blockers = db.execute("""SELECT needs,COUNT(DISTINCT job_id) jobs FROM answer_blockers WHERE resolved_at IS NULL
                GROUP BY needs ORDER BY jobs DESC LIMIT 15""").fetchall()
            if blockers:
                print('Open answer blockers (jobs waiting):')
                for b in blockers:
                    print(f"  {b['jobs']:3} {b['needs']}")
        elif action == 'set':
            value = json.loads(args.value) if args.json else args.value
            fact = kb.put_fact(db, args.key, value, scope=args.scope, source='candidate', verification=kb.USER_CONFIRMED)
            print(f'Saved {fact.ref}')
        elif action == 'confirm-skill':
            kb.confirm_skill(db, args.skill, args.start, args.end, employment_id=args.employment, context=args.context)
            years = kb.skill_experience(db, [args.skill], contexts=(args.context,))
            print(f'Confirmed {kb.canonical_skill(args.skill)}: {years[0]:.2f} years ({args.context})')
        elif action == 'consent':
            if args.kind not in CONSENT_KINDS:
                raise SystemExit('Consent kinds: ' + ', '.join(CONSENT_KINDS))
            kb.put_fact(db, 'consent.' + args.kind, 'granted' if args.grant else 'denied', source='candidate',
                        verification=kb.USER_CONFIRMED)
            print(f"Standing consent {args.kind}: {'granted' if args.grant else 'denied'}")
        elif action == 'learn':
            kb.learn_template(db, args.question, args.intent)
            print(f'Wording mapped to {args.intent}; no personal answer was saved.')
        elif action == 'legacy':
            for f in kb.legacy_facts(db)[: args.limit]:
                print(f"  {f.key[:90]} = {str(f.value)[:60]}")
    finally:
        db.close()


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    parser = argparse.ArgumentParser(prog="careerops", description="Local-first job discovery and application operations")
    sub = parser.add_subparsers(dest="command", required=True)
    remember = sub.add_parser('remember-answer')
    remember.add_argument('--question', required=True)
    remember.add_argument('--answer', required=True)
    remember.set_defaults(handler=cmd_remember_answer)
    facts = sub.add_parser('facts', help='Review and confirm candidate facts for screening answers')
    facts.set_defaults(handler=cmd_facts)
    fsub = facts.add_subparsers(dest='facts_action', required=True)
    fsub.add_parser('import'); fsub.add_parser('review')
    fset = fsub.add_parser('set'); fset.add_argument('key'); fset.add_argument('value')
    fset.add_argument('--scope', default='global'); fset.add_argument('--json', action='store_true')
    fskill = fsub.add_parser('confirm-skill'); fskill.add_argument('skill'); fskill.add_argument('--start', required=True, help='YYYY-MM')
    fskill.add_argument('--end', help='YYYY-MM; omit for current'); fskill.add_argument('--employment')
    fskill.add_argument('--context', default='professional', choices=['professional', 'personal_project', 'research'])
    fconsent = fsub.add_parser('consent'); fconsent.add_argument('kind')
    group = fconsent.add_mutually_exclusive_group(required=True)
    group.add_argument('--grant', action='store_true'); group.add_argument('--deny', action='store_true')
    flearn = fsub.add_parser('learn'); flearn.add_argument('question'); flearn.add_argument('intent')
    flegacy = fsub.add_parser('legacy'); flegacy.add_argument('--limit', type=int, default=50)
    for name, handler in (("doctor",cmd_doctor),("init",cmd_init),("discover",cmd_discover),("naukri-login",cmd_naukri_login),("naukri-discover",cmd_naukri_discover),("portal-login",cmd_portal_login),("score",cmd_score),("queue",cmd_queue),("apply",cmd_apply),("apply-batch",cmd_apply_batch),("retry",cmd_retry),("export",cmd_export),("status",cmd_status),("run",cmd_run)):
        p = sub.add_parser(name); p.set_defaults(handler=handler)
        if name in ("queue", "run"): p.add_argument("--limit", type=int)
        if name == "run": p.add_argument("--apply", action="store_true", help="Apply queued jobs using the configured unattended adapters after discovery")
        if name == "apply":
            p.add_argument("--human-challenges", action="store_true", help="Keep Chrome visible and pause for human verification")
            p.add_argument("--dry-run", action="store_true", help="Fill this job and report blockers without submitting or pausing")
            p.add_argument("--job", type=int, help="Job ID from the tracker; defaults to highest-scoring queued job")
            p.add_argument("--confirm-fill", action="store_true", help="Confirm transmitting profile fields and resume to the selected employer form")
        if name == "apply-batch":
            p.add_argument("--human-challenges", action="store_true", help="Keep Chrome visible and pause for human verification")
            p.add_argument("--dry-run", action="store_true", help="Fill and report missing questions without submitting or pausing")
            p.add_argument("--limit", type=int, default=10)
        if name == "retry":
            p.add_argument("--captcha", action="store_true", help="Also requeue CAPTCHA-blocked jobs")
            p.add_argument("--all", action="store_true", help="Requeue all fixable and blocked jobs")
        if name == "naukri-discover":
            p.add_argument("--keywords", default="software engineer")
            p.add_argument("--location", default="India")
            p.add_argument("--pages", type=int, default=2)
            p.add_argument("--headless", action="store_true", help="Run without showing Chrome; visible mode is more reliable")
        if name == "portal-login":
            p.add_argument("--url", required=True, help="Portal sign-in URL to open in the persistent application browser")
    args = parser.parse_args(); args.handler(args)


if __name__ == "__main__": main()
