"""Bounded lead discovery for job portals without a validated submit adapter.

The portals are treated as lead sources. Results are scored by JobPilot and
queued with the original listing URL; application submission is only enabled
when the URL resolves to a tested employer ATS adapter.
"""
from __future__ import annotations

import html
import re
from dataclasses import asdict
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import jobpilot

PORTALS = {
    "wellfound": ("Wellfound", "https://wellfound.com/jobs?query={query}&location=India"),
    "instahyre": ("Instahyre", "https://www.instahyre.com/search-jobs/?search={query}"),
    "hirist": ("Hirist.tech", "https://www.hirist.tech/search/{query}-jobs"),
    "trueup": ("TrueUp", "https://www.trueup.io/jobs?search={query}&location=India"),
    "offcampus_jobs4u": ("OffCampus Jobs 4U", "https://www.offcampusjobs4u.com/?s={query}"),
}

# Google Jobs has a user-facing search page but JobPilot has no permitted
# bulk feed/API for its result cards. Keep it as a one-click search handoff.
GOOGLE_JOBS_SEARCH_URL = "https://www.google.com/search?ibp=htl;jobs&q={query}"

DOMAINS = {
    "wellfound": "wellfound.com",
    "instahyre": "instahyre.com",
    "hirist": "hirist.tech",
    "trueup": "trueup.io",
    "offcampus_jobs4u": "offcampusjobs4u.com",
}


def _terms() -> list[str]:
    profile = jobpilot.load_json("profile.json")
    roles = profile.get("preferences", {}).get("roles", [])
    return roles or ["backend engineer", "dotnet developer", "full stack developer", "platform engineer"]


def _rss(query: str, domain: str | None = None) -> list[dict]:
    scoped = f"site:{domain} {query}" if domain else query
    # Google News RSS is a public, bounded search surface. It returns links
    # that can be kept as leads even when a portal's own UI is JavaScript-only.
    url = "https://news.google.com/rss/search?q=" + quote_plus(scoped) + "&hl=en-IN&gl=IN&ceid=IN:en"
    request = Request(url, headers={"User-Agent": "JobPilot/1.0 (job discovery)"})
    with urlopen(request, timeout=20) as response:
        root = ElementTree.fromstring(response.read())
    rows = []
    entries = root.findall("{http://www.w3.org/2005/Atom}entry") or root.findall(".//item")
    for entry in entries:
        title = html.unescape(entry.findtext("{http://www.w3.org/2005/Atom}title", entry.findtext("title", ""))).strip()
        link = entry.find("{http://www.w3.org/2005/Atom}link")
        href = (link.get("href", "") if link is not None else entry.findtext("link", "")).strip()
        summary = html.unescape(entry.findtext("{http://www.w3.org/2005/Atom}content", entry.findtext("description", ""))).strip()
        source = entry.find("source")
        source_url = (source.get("url", "") if source is not None else "").strip()
        if title and href.startswith("http"):
            rows.append({"title": title, "url": href, "description": re.sub(r"<[^>]+>", " ", summary), "source_url": source_url})
    return rows


def discover(limit: int = 20, portals: list[str] | None = None) -> dict:
    portals = portals or list(PORTALS)
    db = jobpilot.connect()
    imported = duplicates = errors = succeeded = 0
    try:
        for portal in portals:
            if portal not in PORTALS:
                continue
            label, _ = PORTALS[portal]
            domain = DOMAINS.get(portal, "")
            for term in _terms()[:4]:
                try:
                    rows = _rss(f"{term} India remote jobs", domain)[:limit]
                    with db:
                        for row in rows:
                            host = (urlparse(row["url"]).hostname or "").lower()
                            source_host = (urlparse(row.get("source_url", "")).hostname or "").lower()
                            if portal != "google_jobs" and (domain not in host and domain not in source_host and domain not in row["title"].lower()):
                                continue
                            job = jobpilot.Job(
                                row["url"], label, row["title"], row["description"],
                                "India / Remote", label, row["url"], row["url"], portal,
                            )
                            before = db.total_changes
                            db.execute("""INSERT OR IGNORE INTO jobs
                                (external_id,company,title,description,location,source,source_url,apply_url,ats,posted_at,discovered_at,fingerprint,description_hash)
                                VALUES(:external_id,:company,:title,:description,:location,:source,:source_url,:apply_url,:ats,:posted_at,:discovered_at,:fingerprint,:description_hash)""",
                                asdict(job) | {"discovered_at": jobpilot.now(), "fingerprint": job.fingerprint,
                                               "description_hash": job.description_hash})
                            if db.total_changes > before:
                                imported += 1
                            else:
                                duplicates += 1
                    succeeded += 1
                except Exception as exc:
                    errors += 1
                    print(f"{portal} discovery failed: {type(exc).__name__}: {exc}", flush=True)
    finally:
        db.close()
    return {"imported": imported, "duplicates": duplicates, "errors": errors, "succeeded": succeeded, "portals": portals}


if __name__ == "__main__":
    print(discover())
