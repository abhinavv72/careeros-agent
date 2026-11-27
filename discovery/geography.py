"""Geographic residence and work arrangement parsing.

Separates remote work style from permitted country residence.
Does not infer worldwide or India eligibility from the bare word 'remote'.
"""
from __future__ import annotations

import re
from typing import Optional
from discovery.models import WorkArrangement

# Regex patterns for country detection
INDIA_PATTERNS = [
    r"\bindia\b",
    r"\bbengaluru\b",
    r"\bbangalore\b",
    r"\bhyderabad\b",
    r"\bpune\b",
    r"\bmumbai\b",
    r"\bdelhi\b",
    r"\bgurugram\b",
    r"\bgurgaon\b",
    r"\bnoida\b",
    r"\bchennai\b",
    r"\bapac\b",
    r"\basia.?pacific\b",
]

FOREIGN_RESTRICTION_PATTERNS = [
    (r"\b(united states|usa|u\.s\.a?|us only|us-only|us)\b", "US"),
    (r"\bcroatia\b", "Croatia"),
    (r"\bcanada\b", "Canada"),
    (r"\buk\b|\bunited kingdom\b|\blondon\b", "UK"),
    (r"\beurope\b|\beea\b|\beu\b", "Europe"),
    (r"\bgermany\b|\bberlin\b", "Germany"),
    (r"\bfrance\b|\bparis\b", "France"),
    (r"\baustralia\b|\bsydney\b|\bmelbourne\b", "Australia"),
    (r"\blatam\b|\blatin america\b", "LATAM"),
]

WORLDWIDE_PATTERNS = [
    r"\bworldwide\b",
    r"\bglobal\b",
    r"\banywhere\b",
    r"\bwork from anywhere\b",
    r"\bremote - worldwide\b",
    r"\bremote \(worldwide\)\b",
    r"\bremote - global\b",
    r"\bremote \(global\)\b",
    r"\banywhere in the world\b",
]


def parse_work_arrangement(raw_location: str, description_text: str = "") -> WorkArrangement:
    """Evaluate work arrangement and geography from location string and optional text snippet.

    Rules:
    - 'remote' keyword indicates arrangement_type == 'remote' (or hybrid if stated).
    - Bare 'remote' without country or worldwide indicators is marked unknown permitted_geography,
      is_country_restricted=False, is_india_eligible=None (requires human/profile review).
    - Foreign restricted locations (e.g. 'Remote, US', 'US Only', 'Remote (Croatia)')
      are marked is_country_restricted=True, is_india_eligible=False.
    - India locations (e.g. 'India (Remote)', 'Bengaluru, India', 'Remote - India')
      are marked is_india_eligible=True, permitted_geography='India'.
    - Worldwide locations are marked is_country_restricted=False, is_india_eligible=True,
      permitted_geography='Worldwide'.
    """
    loc_clean = (raw_location or "").strip()
    loc_lower = loc_clean.lower()
    desc_sample = (description_text or "")[:1500].lower()

    # Determine arrangement type (remote / hybrid / onsite / unknown)
    is_explicit_worldwide = any(re.search(pat, loc_lower) for pat in WORLDWIDE_PATTERNS)

    if "hybrid" in loc_lower or "hybrid" in desc_sample[:300]:
        arr_type = "hybrid"
    elif "remote" in loc_lower or "work from home" in loc_lower or "telecommute" in loc_lower or is_explicit_worldwide:
        arr_type = "remote"
    elif any(term in loc_lower for term in ["onsite", "on-site", "office"]):
        arr_type = "onsite"
    elif not loc_clean:
        arr_type = "unknown"
    else:
        # Default arrangement when location is given as city/country
        arr_type = "onsite" if "remote" not in desc_sample[:300] else "remote"

    # 1. Check for Worldwide / Global
    for pattern in WORLDWIDE_PATTERNS:
        if re.search(pattern, loc_lower):
            return WorkArrangement(
                arrangement_type=arr_type,
                permitted_geography="Worldwide",
                is_country_restricted=False,
                is_india_eligible=True,
                raw_location=loc_clean,
            )

    # 2. Check for India
    is_in_india = any(re.search(pat, loc_lower) for pat in INDIA_PATTERNS)
    if is_in_india:
        # Check if India is explicitly combined with a foreign only restriction (rare, but be safe)
        has_foreign = any(re.search(pat, loc_lower) for pat, _ in FOREIGN_RESTRICTION_PATTERNS)
        if not has_foreign:
            return WorkArrangement(
                arrangement_type=arr_type,
                permitted_geography="India",
                is_country_restricted=True,
                is_india_eligible=True,
                raw_location=loc_clean,
            )

    # 3. Check for specific foreign restriction
    for pattern, geo in FOREIGN_RESTRICTION_PATTERNS:
        if re.search(pattern, loc_lower):
            return WorkArrangement(
                arrangement_type=arr_type,
                permitted_geography=geo,
                is_country_restricted=True,
                is_india_eligible=False,
                raw_location=loc_clean,
            )

    # Also check description for explicit country restrictions if location was bare remote
    if arr_type == "remote":
        for pattern, geo in FOREIGN_RESTRICTION_PATTERNS:
            if re.search(r"\b(must be located in|reside in|only hiring in|eligible to work in)\s+" + pattern, desc_sample):
                return WorkArrangement(
                    arrangement_type=arr_type,
                    permitted_geography=geo,
                    is_country_restricted=True,
                    is_india_eligible=False,
                    raw_location=loc_clean,
                )

    # 4. If bare remote or unknown location
    if arr_type == "remote":
        # The location says "Remote" or similar with no recognized country
        return WorkArrangement(
            arrangement_type="remote",
            permitted_geography="Unknown",
            is_country_restricted=False,
            is_india_eligible=None,  # Review needed, do NOT assume eligible
            raw_location=loc_clean,
        )

    # 5. Non-remote unknown location
    return WorkArrangement(
        arrangement_type=arr_type,
        permitted_geography="Unknown" if not loc_clean else loc_clean,
        is_country_restricted=False if not loc_clean else True,
        is_india_eligible=None if not loc_clean else False,
        raw_location=loc_clean,
    )
