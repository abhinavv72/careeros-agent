"""Automated fast retrieval of email verification codes for job applications."""
import email
import email.utils
import imaplib
import re
import time
from datetime import datetime
from pathlib import Path

def get_gmail_credentials():
    path = Path("config/secrets.env")
    if not path.is_file():
        return None, None
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().replace(" ", "")
    return env.get("GMAIL_USER"), env.get("GMAIL_APP_PASSWORD")

_NOT_CODES = {"security", "complete", "continue", "received", "feedback", "engineer", "software", "contract",
              "recruiter", "resubmit", "overflow", "tracking", "pipeline", "platform", "ashbyhq", "greenhouse"}


def extract_verification_code(text, expected_length):
    """Return (code, how) for an exact-length code token, or (None, None).

    The code must end at a token boundary: the first characters of a longer number
    (reference ID, phone number) are never returned as a code.
    """
    n = int(expected_length)
    contextual = re.search(
        rf"(?:security code.*?:\s*|verification code.*?:\s*|code\s*(?:is|:)\s*)([A-Za-z0-9]{{{n}}})(?![A-Za-z0-9])",
        text, re.I)
    if contextual and contextual.group(1).lower() not in _NOT_CODES:
        return contextual.group(1), "contextual"
    for candidate in re.findall(rf"\b([A-Za-z0-9]{{{n}}})\b", text):
        if candidate.lower() not in _NOT_CODES:
            return candidate, "matching"
    return None, None


def fetch_latest_verification_code(expected_length=8, timeout_seconds=30, since_timestamp=None):
    user, pwd = get_gmail_credentials()
    if not user or not pwd:
        print("[gmail] No credentials in config/secrets.env", flush=True)
        return None

    start_time = time.monotonic()
    search_since = since_timestamp if since_timestamp is not None else (time.time() - 600)

    print(f"[gmail] Listening for {expected_length}-character verification code (timeout: {timeout_seconds}s)...", flush=True)

    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=10)
        mail.login(user, pwd)
        mail.select("INBOX")
    except Exception as e:
        print(f"[gmail] Connection failed: {e}", flush=True)
        return None

    try:
        while time.monotonic() - start_time < timeout_seconds:
            try:
                # Fetch recent messages from inbox (robust across timezones)
                status, data = mail.search(None, "ALL")
                ids = data[0].split()[-20:] if status == "OK" and data and data[0] else []

                for msg_id in reversed(ids):
                    # Step 1: Fast header peek
                    st, d = mail.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
                    if not d or not d[0] or not isinstance(d[0], tuple):
                        continue
                    header_text = d[0][1].decode('utf-8', errors='ignore')
                    
                    # Parse subject and from
                    subj_match = re.search(r"^Subject:\s*(.*)$", header_text, re.M | re.I)
                    from_match = re.search(r"^From:\s*(.*)$", header_text, re.M | re.I)
                    date_match = re.search(r"^Date:\s*(.*)$", header_text, re.M | re.I)

                    subject = subj_match.group(1).strip() if subj_match else ""
                    sender = from_match.group(1).strip() if from_match else ""
                    
                    if date_match and search_since > 0:
                        try:
                            date_tuple = email.utils.parsedate_to_datetime(date_match.group(1).strip())
                            if date_tuple and date_tuple.timestamp() < search_since - 60:
                                continue
                        except Exception:
                            pass

                    # Only inspect full body if relevant header; ignore submission receipts
                    if re.search(r"thank you for applying|received your application|application confirmation", subject, re.I):
                        continue
                    if not re.search(r"security|verification|code", f"{subject}", re.I):
                        continue

                    # Step 2: Fetch full message
                    st, d = mail.fetch(msg_id, "(RFC822)")
                    if not d or not d[0] or not isinstance(d[0], tuple):
                        continue
                    msg = email.message_from_bytes(d[0][1])

                    body = ""
                    if msg.is_multipart():
                        for p in msg.walk():
                            if p.get_content_type() in ("text/plain", "text/html"):
                                pl = p.get_payload(decode=True)
                                if pl:
                                    body += pl.decode("utf-8", errors="ignore") + "\n"
                    else:
                        pl = msg.get_payload(decode=True)
                        if pl:
                            body = pl.decode("utf-8", errors="ignore")

                    clean_body = re.sub(r"<style[\s\S]*?</style>", " ", body, flags=re.I)
                    clean_body = re.sub(r"<[^>]+>", " ", clean_body)
                    clean_text = " ".join(f"{subject} {clean_body}".split())

                    code, how = extract_verification_code(clean_text, expected_length)
                    if code:
                        print(f"[gmail] Found {how} verification code ({len(code)} chars) from '{sender}' (Subject: {subject})", flush=True)
                        return code

            except Exception as e:
                print(f"[gmail] Polling note: {e}", flush=True)

            time.sleep(3)
            try:
                mail.noop()
            except Exception:
                try:
                    mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=10)
                    mail.login(user, pwd)
                    mail.select("INBOX")
                except Exception:
                    pass
    finally:
        try:
            if mail:
                mail.logout()
        except Exception:
            pass

    print("[gmail] Timed out waiting for verification code email", flush=True)
    return None

if __name__ == "__main__":
    code = fetch_latest_verification_code(expected_length=8, timeout_seconds=5)
    print("Result:", code)
