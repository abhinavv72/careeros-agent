"""Job Diary and Email Reading Service for JobPilot.

Connects to candidate's Gmail via IMAP to read job-related emails,
classifies them (interview invites, rejections, HR outreach, receipts),
maintains an auditable Job Diary with pending follow-ups, and drafts
personalized recruiter follow-ups.
"""
from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jobpilot
from gmail_verifier import get_gmail_credentials

ROOT = Path(__file__).resolve().parent
DATA = jobpilot.DATA
LOGGER = logging.getLogger("jobpilot.diary")


def init_diary_db():
    """Ensure SQLite tables for the Job Diary and Synced Emails exist."""
    db = jobpilot.connect()
    try:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS job_diary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type TEXT NOT NULL, /* 'hr_call', 'interview_scheduled', 'interview_given', 'followup_pending', 'rejection', 'offer', 'note' */
            company TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT '',
            contact_name TEXT NOT NULL DEFAULT '',
            contact_email TEXT NOT NULL DEFAULT '',
            contact_phone TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            action_items TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', /* 'pending', 'completed', 'scheduled', 'rejected', 'archived' */
            due_date TEXT,
            source TEXT NOT NULL DEFAULT 'manual', /* 'manual', 'agent_chat', 'email_sync' */
            draft_subject TEXT NOT NULL DEFAULT '',
            draft_body TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS synced_emails (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            sender TEXT NOT NULL,
            sender_email TEXT NOT NULL,
            company TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'other', /* 'interview_invite', 'rejection', 'hr_followup', 'receipt', 'offer', 'job_lead', 'other' */
            snippet TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            action_needed TEXT NOT NULL DEFAULT '',
            received_at TEXT,
            synced_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_diary_status ON job_diary(status, entry_type, due_date);
        CREATE INDEX IF NOT EXISTS idx_diary_company ON job_diary(company, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_synced_emails_cat ON synced_emails(category, synced_at DESC);
        """)
        db.commit()
    finally:
        db.close()


def decode_str(header_val: Optional[str]) -> str:
    """Decode RFC 2047 header strings safely."""
    if not header_val:
        return ""
    try:
        decoded_parts = email.header.decode_header(header_val)
        out = []
        for content, encoding in decoded_parts:
            if isinstance(content, bytes):
                out.append(content.decode(encoding or "utf-8", errors="ignore"))
            else:
                out.append(str(content))
        return " ".join("".join(out).split())
    except Exception:
        return str(header_val)


def extract_name_and_email(from_hdr: str) -> Tuple[str, str]:
    """Extract clean display name and email address from From: header."""
    name, addr = email.utils.parseaddr(from_hdr)
    name = decode_str(name).strip()
    addr = addr.strip().lower()
    if not name and addr:
        name = addr.split("@")[0].replace(".", " ").replace("-", " ").title()
    return name, addr


def extract_company_from_email(from_hdr: str, subject: str, body: str) -> str:
    """Intelligently deduce company name from sender domain or subject text."""
    disp_name, addr = extract_name_and_email(from_hdr)
    subj_clean = decode_str(subject)
    
    # 1. Look for patterns in subject: "application to Canonical", "role at Google", "from Swiggy"
    match = re.search(r"(?:application\s+to|role\s+at|position\s+at|opportunity\s+at|interview\s+with|team\s+at)\s+([A-Za-z0-9\s&]+?)(?:\s*[-–—|:]|\s+for|\.|$)", subj_clean, re.I)
    if match:
        comp = match.group(1).strip()
        if len(comp) > 1 and comp.lower() not in ("your", "the", "a", "an", "our"):
            return comp

    # 2. Look for Naukri / portal recruiter companies in subject: "[Company] is hiring"
    match2 = re.search(r"Job\s*\|\s*.*?\s+at\s+([A-Za-z0-9\s&]+?)(?:\s+For|\s+in|\.|$)", subj_clean, re.I)
    if match2:
        return match2.group(1).strip()

    # 3. Look at sender domain if company specific
    if "@" in addr:
        domain = addr.split("@")[-1].lower()
        parts = domain.split(".")
        # Ignore generic email hosts and ATS relay domains
        generic_hosts = {"gmail", "outlook", "hotmail", "yahoo", "greenhouse-mail", "us", "eu", "lever", "ashbyhq", "myworkday", "naukri", "foundit", "linkedin"}
        comp_candidate = [p for p in parts if p not in ("com", "co", "in", "org", "net", "io", "dev", "ai", "app") and p not in generic_hosts]
        if comp_candidate:
            return comp_candidate[0].title()

    # 4. Fallback to display name if not generic
    if disp_name and not re.search(r"careers|recruitment|talent|notification|no-reply|alerts|updates|jobs", disp_name, re.I):
        return disp_name.split()[0].title()

    return "Employer"


def extract_role_from_text(subject: str, body: str) -> str:
    """Extract role / job title from subject or body snippet."""
    subj = decode_str(subject)
    match = re.search(r"(?:for|role|position)\s*[-–—:]?\s*([A-Za-z0-9\s\+\#\.\/\-]{3,45}?)(?:\s*(?:at|role|position|-|–|—|\(|\.|$))", subj, re.I)
    if match:
        role = match.group(1).strip()
        if len(role) > 3 and not re.search(r"your|further|this|next|update", role, re.I):
            return role
    
    # Check common tech roles in subject
    tech_patterns = [
        r"full\s*stack(?:\s+developer|\s+engineer)?",
        r"software(?:\s+development)?\s+engineer(?:\s+[I|II|III|1|2|3])?",
        r"backend(?:\s+developer|\s+engineer)?",
        r"frontend(?:\s+developer|\s+engineer)?",
        r"python(?:\s+developer|\s+engineer)?",
        r"devops(?:\s+engineer)?",
        r"cloud(?:\s+support|\s+field|\s+engineer)?",
        r"linux(?:\s+devices|\s+security|\s+engineer)?",
        r"data(?:\s+engineer|\s+scientist)?"
    ]
    for pat in tech_patterns:
        m = re.search(pat, f"{subj} {body[:300]}", re.I)
        if m:
            return m.group(0).title()
    return ""


def classify_job_email(subject: str, sender: str, body: str) -> Tuple[str, str, str]:
    """Classify email into (category, action_needed, summary_tag)."""
    text = f"{subject} {body}".lower()
    
    # 1. Offers
    if any(k in text for k in ("offer letter", "job offer", "congratulations on your offer", "pleased to offer you the position")):
        return "offer", "Action needed: Review offer details and response deadline.", "Job Offer Received"

    # 2. Interview Invitations
    if any(k in text for k in ("invitation to interview", "schedule an interview", "schedule a call", "interview confirmation",
                              "technical assessment", "coding assessment", "hackerrank test", "take-home challenge",
                              "technical interview", "first round interview", "video interview", "calendly.com",
                              "meet with the team", "zoom meeting", "google meet link")):
        return "interview_invite", "Urgent: Schedule or confirm interview slot.", "Interview Invitation"

    # 3. Rejections
    if any(k in text for k in ("regret to inform", "not moving forward", "not be progressing", "unfortunately",
                              "pursuing other candidates", "decided to move forward with other", "wishing you the best in your search",
                              "other applicants who more closely", "not selected")):
        return "rejection", "No action needed (Logged in failure & lessons tracker).", "Application Rejection"

    # 4. HR Outreach / Recruiter Follow-up
    if any(k in text for k in ("found your profile", "shortlisted your profile", "talent acquisition", "recruiter",
                              "discuss your background", "availability for a quick call", "let's connect",
                              "regarding your application", "next steps in your process")):
        return "hr_followup", "Action needed: Reply to recruiter with availability and resume.", "Recruiter Follow-up"

    # 5. Application Receipts
    if any(k in text for k in ("thank you for applying", "received your application", "application confirmation",
                              "we have received your application", "application submitted successfully")):
        return "receipt", "Application acknowledged by ATS.", "Application Receipt"

    # 6. Job leads from portals
    if any(k in text for k in ("naukri.com", "foundit", "job alert", "matching jobs")):
        return "job_lead", "Review new opening if interested.", "Job Lead"

    return "other", "", "General Update"


def get_candidate_profile() -> Dict[str, Any]:
    """Load locally configured candidate details for draft generation."""
    profile_path = ROOT / "config" / "profile.json"
    if profile_path.is_file():
        try:
            return json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"personal": {}}


def generate_followup_draft(
    company: str,
    contact_name: str = "",
    role: str = "",
    notes: str = "",
    context: str = "hr_call"
) -> Dict[str, str]:
    """Generate a highly professional, context-aware follow-up email draft."""
    profile = get_candidate_profile()
    pers = profile.get("personal", {})
    first = pers.get("firstName", "")
    last = pers.get("lastName", "")
    full_name = f"{first} {last}".strip()
    phone = pers.get("phone", "")
    email_addr = pers.get("email", "")
    portfolio = pers.get("portfolio", "")
    github = pers.get("github", "")
    linkedin = pers.get("linkedin", "")

    name_suffix = f" - {full_name}" if full_name else ""
    links = "\n".join(f"- {label}: {url}" for label, url in (("Portfolio", portfolio), ("GitHub", github), ("LinkedIn", linkedin)) if url)
    contact = "\n".join(value for value in (f"Phone: {phone}" if phone else "", f"Email: {email_addr}" if email_addr else "") if value)
    salutation = f"Hi {contact_name}," if contact_name and contact_name.lower() not in ("employer", "hr", "recruiter", "") else "Hello Hiring Team,"
    role_str = f" for the {role} position" if role else ""
    comp_str = f" at {company}" if company and company.lower() != "employer" else ""

    if context == "hr_call":
        subject = f"Following Up: Discussion regarding {role or 'Software Engineer'} role{comp_str}{name_suffix}"
        body = f"""{salutation}

Thank you for taking the time to speak with me today regarding the opportunity{role_str}{comp_str}.

I appreciated learning more about the team and the role, and I remain interested in the opportunity.

{f'Note from our call: {notes}' if notes else 'As discussed, I am eager to take the next steps in the evaluation process.'}

{('You can find more about my work here:\n' + links) if links else ''}

I am readily available for subsequent technical rounds or follow-up discussions at your convenience.

Best regards,

{full_name}{('\n' + contact) if contact else ''}
"""

    elif context == "interview_given":
        subject = f"Thank You: Interview for {role or 'Software Engineering'} role{comp_str}{name_suffix}"
        body = f"""{salutation}

Thank you for coordinating our interview session today. I thoroughly enjoyed discussing the technical challenges and architecture behind the work at {company or 'your team'}.

Our conversation reinforced my interest in the role and the team's work.

{f'Key takeaways from our discussion: {notes}' if notes else ''}

Please let me know if there are any additional code samples, artifacts, or references I can provide as your team evaluates next steps.

Looking forward to hearing from you.

Warm regards,

{full_name}{('\n' + contact) if contact else ''}
"""

    elif context == "status_check":
        subject = f"Checking In: Status Update on Application{role_str}{comp_str}{name_suffix}"
        body = f"""{salutation}

I hope you are having a productive week.

I am writing to check in on the status of my application{role_str}{comp_str}. I remain interested in the opportunity and would welcome an update when convenient.

Please let me know if you need any additional information from my side.

Thank you again for your time and consideration.

Best regards,

{full_name}{('\n' + contact) if contact else ''}
"""

    else:
        subject = f"Follow-up: {role or 'Software Engineer'} opportunity{comp_str}{name_suffix}"
        body = f"""{salutation}

Following up on our correspondence regarding the {role or 'Software Engineer'} position{comp_str}.

I am available and excited to connect on next steps.

Best regards,

{full_name}{('\n' + contact) if contact else ''}
"""

    return {
        "subject": subject.strip(),
        "body": body.strip()
    }


def sync_gmail_emails(max_emails: int = 50) -> Dict[str, Any]:
    """Scan candidate's Gmail inbox for job-related emails, classify and log into SQLite."""
    init_diary_db()
    user, pwd = get_gmail_credentials()
    if not user or not pwd:
        return {"error": "No Gmail credentials found in config/secrets.env", "synced": 0}

    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=12)
        mail.login(user, pwd)
        res, count_data = mail.select("INBOX", readonly=True)
        if res != "OK" or not count_data or not count_data[0]:
            return {"error": "Failed to open INBOX", "synced": 0}
        
        total_inbox = int(count_data[0])
        if total_inbox == 0:
            return {"synced": 0, "message": "Inbox empty"}

        fetch_count = min(max_emails, total_inbox)
        start_seq = max(1, total_inbox - fetch_count + 1)
        end_seq = total_inbox

        # Step 1: Bulk fetch headers in single call
        st, header_data = mail.fetch(f"{start_seq}:{end_seq}", "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])")
        if st != "OK" or not header_data:
            return {"error": "Failed to fetch email headers", "synced": 0}

        # Step 2: Query already stored email IDs
        db = jobpilot.connect()
        try:
            existing_ids = {row[0] for row in db.execute("SELECT id FROM synced_emails").fetchall()}
        finally:
            db.close()

        candidate_items = []
        for item in header_data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            hdr_text = item[1].decode("utf-8", errors="ignore")
            
            # Extract basic headers
            subj_m = re.search(r"^Subject:\s*(.*)$", hdr_text, re.M | re.I)
            from_m = re.search(r"^From:\s*(.*)$", hdr_text, re.M | re.I)
            date_m = re.search(r"^Date:\s*(.*)$", hdr_text, re.M | re.I)
            msgid_m = re.search(r"^Message-ID:\s*(.*)$", hdr_text, re.M | re.I)

            subj = decode_str(subj_m.group(1).strip() if subj_m else "")
            sender_raw = decode_str(from_m.group(1).strip() if from_m else "")
            date_str = date_m.group(1).strip() if date_m else ""
            msg_id = msgid_m.group(1).strip() if msgid_m else ""

            if not msg_id:
                # Generate deterministic fallback key
                msg_id = f"gen-{abs(hash((subj, sender_raw, date_str)))}"

            if msg_id in existing_ids:
                continue

            # Quick filter: is this likely job/recruiter related?
            combined_header = f"{subj} {sender_raw}".lower()
            is_job = any(k in combined_header for k in (
                "canonical", "application", "interview", "recruiter", "talent", "hiring", "job", "career",
                "greenhouse", "lever", "ashby", "workday", "naukri", "candidate", "role", "position", "offer"
            ))
            if not is_job:
                continue

            # Extract sequence number from header tuple name e.g. b'41440 (BODY...'
            seq_num = None
            if item[0]:
                seq_match = re.match(r"^(\d+)", item[0].decode("ascii", errors="ignore"))
                if seq_match:
                    seq_num = seq_match.group(1)

            candidate_items.append({
                "msg_id": msg_id,
                "seq_num": seq_num,
                "subject": subj,
                "sender_raw": sender_raw,
                "date_str": date_str
            })

        new_synced_count = 0
        categories_count: Dict[str, int] = {}
        now_iso = jobpilot.now()

        # Step 3: Fetch body for candidates
        db = jobpilot.connect()
        try:
            for item in candidate_items:
                body_text = ""
                if item["seq_num"]:
                    try:
                        bst, bdata = mail.fetch(item["seq_num"], "(RFC822)")
                        if bst == "OK" and bdata and isinstance(bdata[0], tuple):
                            full_msg = email.message_from_bytes(bdata[0][1])
                            if full_msg.is_multipart():
                                for part in full_msg.walk():
                                    if part.get_content_type() in ("text/plain", "text/html"):
                                        pl = part.get_payload(decode=True)
                                        if pl:
                                            body_text += pl.decode("utf-8", errors="ignore") + "\n"
                            else:
                                pl = full_msg.get_payload(decode=True)
                                if pl:
                                    body_text = pl.decode("utf-8", errors="ignore")
                    except Exception as exc:
                        LOGGER.warning("Failed fetching body for email seq %s: %s", item["seq_num"], exc)

                # Clean body
                clean_body = re.sub(r"<style[\s\S]*?</style>", " ", body_text, flags=re.I)
                clean_body = re.sub(r"<script[\s\S]*?</script>", " ", clean_body, flags=re.I)
                clean_body = re.sub(r"<[^>]+>", " ", clean_body)
                clean_snippet = " ".join(clean_body.split())[:350]

                company = extract_company_from_email(item["sender_raw"], item["subject"], clean_body)
                role = extract_role_from_text(item["subject"], clean_body)
                disp_name, email_addr = extract_name_and_email(item["sender_raw"])
                category, action_needed, tag = classify_job_email(item["subject"], item["sender_raw"], clean_body)

                # Store in synced_emails
                db.execute("""
                INSERT OR REPLACE INTO synced_emails
                (id, subject, sender, sender_email, company, role, category, snippet, body, action_needed, received_at, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    item["msg_id"], item["subject"], disp_name, email_addr, company, role,
                    category, clean_snippet, clean_body[:2000], action_needed, item["date_str"], now_iso
                ))
                new_synced_count += 1
                categories_count[category] = categories_count.get(category, 0) + 1

                # Auto-populate or link into Job Diary
                if category == "rejection":
                    summary = f"Application rejected by {company}: {item['subject']}"
                    db.execute("""
                    INSERT INTO job_diary
                    (entry_type, company, role, contact_name, contact_email, summary, action_items, status, due_date, source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        "rejection", company, role, disp_name, email_addr,
                        summary, "Reviewed failure; candidate archive updated.", "completed", None, "email_sync", now_iso, now_iso
                    ))
                elif category == "interview_invite":
                    summary = f"Interview invite from {company}: {item['subject']}"
                    draft = generate_followup_draft(company, disp_name, role, clean_snippet, context="status_check")
                    db.execute("""
                    INSERT INTO job_diary
                    (entry_type, company, role, contact_name, contact_email, summary, action_items, status, due_date, source, draft_subject, draft_body, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        "interview_scheduled", company, role, disp_name, email_addr,
                        summary, "Action needed: Confirm interview date/slot with recruiter.", "scheduled", None, "email_sync",
                        draft["subject"], draft["body"], now_iso, now_iso
                    ))
                elif category == "hr_followup":
                    summary = f"Recruiter outreach from {company}: {item['subject']}"
                    draft = generate_followup_draft(company, disp_name, role, clean_snippet, context="hr_call")
                    db.execute("""
                    INSERT INTO job_diary
                    (entry_type, company, role, contact_name, contact_email, summary, action_items, status, due_date, source, draft_subject, draft_body, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        "hr_call", company, role, disp_name, email_addr,
                        summary, "Action needed: Reply to recruiter discussion.", "pending", None, "email_sync",
                        draft["subject"], draft["body"], now_iso, now_iso
                    ))

            db.commit()
        finally:
            db.close()

        return {
            "synced": new_synced_count,
            "categories": categories_count,
            "checked": len(candidate_items)
        }

    except Exception as exc:
        LOGGER.exception("Gmail sync failed: %s", exc)
        return {"error": str(exc), "synced": 0}
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


def add_diary_entry(
    entry_type: str,
    company: str,
    role: str = "",
    contact_name: str = "",
    contact_email: str = "",
    contact_phone: str = "",
    summary: str = "",
    action_items: str = "",
    status: str = "pending",
    due_date: Optional[str] = None,
    source: str = "manual",
    draft_subject: str = "",
    draft_body: str = ""
) -> int:
    """Add a new entry to the Job Diary."""
    init_diary_db()
    now_iso = jobpilot.now()
    if not draft_body and status == "pending" and entry_type in ("hr_call", "followup_pending"):
        draft = generate_followup_draft(company, contact_name, role, summary, context="hr_call")
        draft_subject = draft["subject"]
        draft_body = draft["body"]

    db = jobpilot.connect()
    try:
        cur = db.execute("""
        INSERT INTO job_diary
        (entry_type, company, role, contact_name, contact_email, contact_phone, summary, action_items, status, due_date, source, draft_subject, draft_body, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry_type, company, role, contact_name, contact_email, contact_phone,
            summary, action_items, status, due_date, source, draft_subject, draft_body, now_iso, now_iso
        ))
        db.commit()
        return cur.lastrowid
    finally:
        db.close()


def update_diary_entry(entry_id: int, **fields) -> bool:
    """Update existing diary entry fields."""
    init_diary_db()
    allowed = {"entry_type", "company", "role", "contact_name", "contact_email", "contact_phone", "summary", "action_items", "status", "due_date", "draft_subject", "draft_body"}
    updates = []
    values = []
    for k, v in fields.items():
        if k in allowed:
            updates.append(f"{k} = ?")
            values.append(v)
    if not updates:
        return False
    updates.append("updated_at = ?")
    values.append(jobpilot.now())
    values.append(entry_id)

    db = jobpilot.connect()
    try:
        cur = db.execute(f"UPDATE job_diary SET {', '.join(updates)} WHERE id = ?", tuple(values))
        db.commit()
        return cur.rowcount > 0
    finally:
        db.close()


def delete_diary_entry(entry_id: int) -> bool:
    """Delete a diary entry."""
    init_diary_db()
    db = jobpilot.connect()
    try:
        cur = db.execute("DELETE FROM job_diary WHERE id = ?", (entry_id,))
        db.commit()
        return cur.rowcount > 0
    finally:
        db.close()


def get_diary_entries(entry_type: Optional[str] = None, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieve filtered diary entries."""
    init_diary_db()
    sql = "SELECT * FROM job_diary"
    where = []
    params = []
    if entry_type:
        where.append("entry_type = ?")
        params.append(entry_type)
    if status:
        where.append("status = ?")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    db = jobpilot.connect()
    try:
        return [dict(row) for row in db.execute(sql, tuple(params)).fetchall()]
    finally:
        db.close()


def get_diary_metrics() -> Dict[str, Any]:
    """Calculate summary counts for the Job Diary dashboard."""
    init_diary_db()
    db = jobpilot.connect()
    try:
        pending_followups = db.execute("SELECT COUNT(*) FROM job_diary WHERE status = 'pending'").fetchone()[0]
        interviews_scheduled = db.execute("SELECT COUNT(*) FROM job_diary WHERE entry_type = 'interview_scheduled' OR status = 'scheduled'").fetchone()[0]
        interviews_given = db.execute("SELECT COUNT(*) FROM job_diary WHERE entry_type = 'interview_given'").fetchone()[0]
        rejections = db.execute("SELECT COUNT(*) FROM job_diary WHERE entry_type = 'rejection'").fetchone()[0]
        hr_calls = db.execute("SELECT COUNT(*) FROM job_diary WHERE entry_type = 'hr_call'").fetchone()[0]
        total_emails = db.execute("SELECT COUNT(*) FROM synced_emails").fetchone()[0]
        last_synced_row = db.execute("SELECT MAX(synced_at) FROM synced_emails").fetchone()
        last_synced = last_synced_row[0] if last_synced_row and last_synced_row[0] else None

        return {
            "pending_followups": pending_followups,
            "interviews_scheduled": interviews_scheduled,
            "interviews_given": interviews_given,
            "rejections": rejections,
            "hr_calls": hr_calls,
            "total_emails": total_emails,
            "last_synced": last_synced
        }
    finally:
        db.close()


def handle_chat_diary_command(text: str) -> Optional[Dict[str, Any]]:
    """Conversational intelligence for HR calls, follow-ups, interviews, and rejections."""
    init_diary_db()
    lower = text.strip().casefold()

    # 1. Intent: Log HR call / recruiter interaction (+ draft follow-up)
    # E.g.: "this hr called me today...", "agent this hr called me and followup with them"
    hr_call_triggers = ("hr called me", "hr reached out", "recruiter called", "recruiter reached out", "spoke with hr", "call with hr", "hr from", "recruiter from")
    if any(k in lower for k in hr_call_triggers) or (lower.startswith("agent ") and "called me" in lower):
        # Extract Company
        company = "Employer"
        comp_m = re.search(r"(?:from|at)\s+([A-Za-z0-9\s&]+?)(?:\s+(?:today|about|regarding|for|and|to)|\.|$)", text, re.I)
        if comp_m:
            c = comp_m.group(1).strip()
            if c.lower() not in ("a", "an", "the", "my", "our", "him", "her", "them", "today", "yesterday"):
                company = c

        # Extract Contact Name
        contact = ""
        contact_m = re.search(r"(?:named|name is|hr|recruiter)\s+([A-Z][a-z]+)", text)
        if contact_m:
            contact = contact_m.group(1).strip()

        # Extract Role
        role = ""
        role_m = re.search(r"(?:about|regarding|for)\s+(?:the\s+)?([A-Za-z0-9\s\+\#\.\-]+?)(?:\s+(?:role|position|opportunity|job|and)|\.|$)", text, re.I)
        if role_m:
            r = role_m.group(1).strip()
            if len(r) > 2 and r.lower() not in ("them", "it", "today", "yesterday", "me", "followup", "follow up"):
                role = r

        followup_requested = any(w in lower for w in ("followup", "follow-up", "follow up", "email them", "send email", "reach out"))
        
        # Build Diary Entry
        summary = text.strip()
        status = "pending"
        entry_id = add_diary_entry(
            entry_type="hr_call",
            company=company,
            role=role,
            contact_name=contact,
            summary=summary,
            action_items="Follow-up with recruiter on discussion points and next steps." if followup_requested else "HR Call logged.",
            status=status,
            source="agent_chat"
        )

        draft = generate_followup_draft(company, contact, role, summary, context="hr_call")
        # Update entry with draft
        update_diary_entry(entry_id, draft_subject=draft["subject"], draft_body=draft["body"])

        reply = (
            f"✅ **Logged in your Job Diary** (Entry #{entry_id})\n\n"
            f"• **Company:** {company}\n"
            f"• **Role:** {role or 'Software Engineer'}\n"
            f"• **Contact:** {contact or 'Recruiter'}\n"
            f"• **Status:** 📌 Pending Follow-up\n\n"
        )
        if followup_requested or True:
            reply += (
                f"✉️ **Tailored Follow-up Draft Ready:**\n\n"
                f"**Subject:** `{draft['subject']}`\n\n"
                f"```text\n{draft['body']}\n```\n"
                f"💡 *You can copy this draft directly or view it in the **Job Diary & Follow-ups** section!*"
            )
        return {
            "reply": reply,
            "action": "diary_entry_created",
            "entry_id": entry_id,
            "draft": draft
        }

    # 2. Intent: Query Pending Follow-ups
    # E.g.: "what are my pending job related followups", "show pending followups", "pending followups"
    if any(k in lower for k in ("pending job related followups", "pending followups", "what are my pending followups", "pending followup", "show followups", "followup tasks")):
        entries = get_diary_entries(status="pending", limit=10)
        if not entries:
            return {
                "reply": "🎉 You have no pending job-related follow-ups right now! Everything is up to date.",
                "action": None
            }
        lines = [f"📋 **You have {len(entries)} pending follow-up{'s' if len(entries) > 1 else ''}:**\n"]
        for e in entries:
            due = f" (Due: {e['due_date']})" if e.get("due_date") else ""
            contact_info = f" — Contact: {e['contact_name']}" if e.get("contact_name") else ""
            lines.append(f"• **{e['company']}** ({e['role'] or e['entry_type'].replace('_', ' ').title()}){contact_info}{due}")
            lines.append(f"  *Note:* {e['summary'][:120]}")
            if e.get("draft_subject"):
                lines.append(f"  *Draft ready:* `{e['draft_subject']}`")
            lines.append("")
        lines.append("💡 *Tip: Visit the **Job Diary & Follow-ups** tab to review email drafts or mark entries done.*")
        return {
            "reply": "\n".join(lines),
            "action": None,
            "items": entries
        }

    # 3. Intent: Interviews Given / Completed / Scheduled
    # E.g.: "what interviews have i given", "interviews given", "show interviews", "given interview"
    if any(k in lower for k in ("interviews given", "what interviews have i given", "interviews attended", "interviews completed", "given interview", "scheduled interviews")):
        interviews = get_diary_entries(entry_type="interview_given", limit=10)
        scheduled = get_diary_entries(entry_type="interview_scheduled", limit=10)
        total = len(interviews) + len(scheduled)
        if total == 0:
            return {
                "reply": "You have no recorded interviews yet in your Job Diary. You can log one by telling me e.g. *“I gave an interview with Swiggy today for Senior Backend role”* or by syncing your emails.",
                "action": None
            }
        lines = [f"🎙️ **Interview Tracker ({total} total):**\n"]
        if scheduled:
            lines.append("**📅 Upcoming / Scheduled:**")
            for s in scheduled:
                lines.append(f"• **{s['company']}** — {s['role'] or 'Software Engineer'} (Status: {s['status'].title()})")
                lines.append(f"  *Details:* {s['summary'][:120]}")
            lines.append("")
        if interviews:
            lines.append("**✅ Interviews Given / Completed:**")
            for i in interviews:
                lines.append(f"• **{i['company']}** — {i['role'] or 'Software Engineer'} (Recorded: {i['created_at'][:10]})")
                lines.append(f"  *Notes:* {i['summary'][:120]}")
            lines.append("")
        return {
            "reply": "\n".join(lines),
            "action": None
        }

    # 4. Intent: Failures / Rejections / Lessons
    # E.g.: "failure given interview", "failures", "show rejections", "what jobs rejected me", "my failures"
    if any(k in lower for k in ("failures", "failure", "rejections", "rejected jobs", "show failures", "show rejections", "why did i fail")):
        rejections = get_diary_entries(entry_type="rejection", limit=15)
        db = jobpilot.connect()
        try:
            tracker_rejections = db.execute("SELECT company, title, eligibility_reason FROM jobs WHERE status = 'Rejected' LIMIT 5").fetchall()
        finally:
            db.close()

        if not rejections and not tracker_rejections:
            return {
                "reply": "No rejections or failed attempts recorded yet! As emails are synced or interviews logged, failure notes and lessons will be cataloged here.",
                "action": None
            }

        lines = [f"❌ **Application & Interview Outcomes / Failures Tracker:**\n"]
        if rejections:
            lines.append("**Logged Updates & Emails:**")
            for r in rejections[:8]:
                lines.append(f"• **{r['company']}** — {r['role'] or 'Position'}: {r['summary'][:120]}")
            lines.append("")
        if tracker_rejections:
            lines.append("**Tracker Filtered Out / Rejected:**")
            for tr in tracker_rejections:
                lines.append(f"• **{tr[0]}** ({tr[1]}): {tr[2] or 'Requirements mismatch'}")
            lines.append("")
        lines.append("💡 *Every rejection provides feedback. Check the **Job Diary** tab for full message logs.*")
        return {
            "reply": "\n".join(lines),
            "action": None
        }

    # 5. Intent: Sync Emails
    # E.g.: "sync emails", "read my emails", "check emails", "fetch emails"
    if any(k in lower for k in ("sync emails", "read emails", "check emails", "scan emails", "fetch emails")):
        sync_res = sync_gmail_emails(max_emails=40)
        if "error" in sync_res:
            return {
                "reply": f"⚠️ Could not sync emails: {sync_res['error']}. Please verify credentials in `config/secrets.env`.",
                "action": None
            }
        cats = sync_res.get("categories", {})
        cat_summary = ", ".join(f"{count} {cat.replace('_', ' ')}" for cat, count in cats.items()) or "no new job emails found"
        return {
            "reply": f"📬 **Gmail Inbox Scan Complete!**\n\nScanned recent emails and found {sync_res['synced']} new job-related messages ({cat_summary}). Any interviews, follow-ups, and rejections have been logged to your Job Diary.",
            "action": "emails_synced",
            "sync_details": sync_res
        }

    return None
