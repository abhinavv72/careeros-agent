"""URL canonicalization and stable job deduplication."""
from __future__ import annotations

import re
import urllib.parse
from typing import Dict, List, Tuple
from discovery.models import DeduplicationResult, NormalizedJob

# Tracking and session query parameters to strip
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "lever-source", "ref", "source", "src", "fbclid", "gclid",
    "trk", "from", "v", "sid"
}


def normalize_apply_url(url: str) -> str:
    """Normalize apply URL by stripping query tracking, fragments, and standardizing host/path."""
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url.strip())
    # Keep only non-tracking query parameters
    query_dict = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    cleaned_query = {k: v for k, v in query_dict.items() if k.lower() not in TRACKING_PARAMS}
    
    # Sort remaining query keys for determinism
    encoded_query = urllib.parse.urlencode(cleaned_query, doseq=True)
    
    # Strip trailing slash from path
    path = parsed.path.rstrip("/")
    if not path:
        path = "/"

    # Lowercase scheme and netloc
    normalized = urllib.parse.urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        path,
        "",  # params
        encoded_query,
        ""   # fragment stripped
    ))
    return normalized


def deduplicate_jobs(jobs: List[NormalizedJob]) -> DeduplicationResult:
    """Deduplicate normalized jobs by normalized apply_url and (employer, title, location).

    Retains provenance across multiple sources and explains why items were dropped.
    """
    seen_urls: Dict[str, NormalizedJob] = {}
    seen_fingerprints: Dict[Tuple[str, str, str], NormalizedJob] = {}
    unique: List[NormalizedJob] = []
    reasons: Dict[str, str] = {}
    duplicates_removed = 0
    provenance_merged = 0

    for job in jobs:
        norm_url = job.normalized_apply_url or normalize_apply_url(job.apply_url)
        norm_employer = re.sub(r"\s+", " ", job.employer.lower()).strip()
        norm_title = re.sub(r"\s+", " ", job.title.lower()).strip()
        norm_loc = re.sub(r"\s+", " ", job.work_arrangement.raw_location.lower()).strip()
        fingerprint = (norm_employer, norm_title, norm_loc)

        # Check URL duplication
        existing = seen_urls.get(norm_url)
        if existing is not None:
            duplicates_removed += 1
            reasons[job.apply_url] = f"Duplicate of {existing.apply_url} by normalized URL: {norm_url}"
            # Merge provenance sources
            if job.source not in existing.provenance_sources:
                existing.provenance_sources.append(job.source)
                provenance_merged += 1
            continue

        # Check content fingerprint duplication
        if fingerprint in seen_fingerprints:
            existing = seen_fingerprints[fingerprint]
            duplicates_removed += 1
            reasons[job.apply_url] = f"Duplicate of {existing.apply_url} by employer/title/location fingerprint"
            if job.source not in existing.provenance_sources:
                existing.provenance_sources.append(job.source)
                provenance_merged += 1
            continue

        # First time seeing this job
        if job.source not in job.provenance_sources:
            job.provenance_sources.append(job.source)

        seen_urls[norm_url] = job
        seen_fingerprints[fingerprint] = job
        unique.append(job)

    return DeduplicationResult(
        unique_jobs=unique,
        duplicates_removed=duplicates_removed,
        provenance_merged=provenance_merged,
        reasons=reasons,
    )
