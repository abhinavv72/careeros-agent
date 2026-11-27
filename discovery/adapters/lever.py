"""Lever adapter — fetches and normalizes jobs from Lever API."""
from __future__ import annotations

import json
import re
import urllib.request
from html import unescape
from typing import Any, Dict, Iterable

from discovery.base import BaseAdapter
from discovery.dedup import normalize_apply_url
from discovery.geography import parse_work_arrangement
from discovery.models import NormalizedJob

_BASE = "https://api.lever.co/v0/postings"


def _plain_html(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _fetch(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "JobPilot/1.0 personal job search"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


class LeverAdapter(BaseAdapter):
    """Lever public Postings API."""

    def fetch_jobs(self, company_slug: str) -> Iterable[dict]:
        postings = _fetch(f"{self.config.get('base_url', _BASE)}/{company_slug}?mode=json")
        yield from postings

    def normalize(self, raw: dict, company_slug: str) -> NormalizedJob:
        raw_id = str(raw.get("id", ""))
        text = raw.get("text", "").strip()
        desc = _plain_html(raw.get("descriptionPlain", "") or raw.get("description", ""))
        
        categories = raw.get("categories", {})
        raw_loc = categories.get("location", "")
        workplace_type = raw.get("workplaceType", "")
        if workplace_type and workplace_type.lower() == "remote" and "remote" not in raw_loc.lower():
            raw_loc = f"Remote - {raw_loc}" if raw_loc else "Remote"

        apply_url = raw.get("hostedUrl", "") or raw.get("applyUrl", "")
        norm_url = normalize_apply_url(apply_url)
        # Lever created/updated timestamp is in milliseconds
        created_at_ms = raw.get("createdAt")
        posted_at = ""
        if created_at_ms:
            from datetime import datetime, timezone
            try:
                posted_at = datetime.fromtimestamp(created_at_ms / 1000, timezone.utc).isoformat()
            except Exception:
                posted_at = str(created_at_ms)

        arrangement = parse_work_arrangement(raw_loc, desc)

        return NormalizedJob(
            external_id=raw_id,
            employer=company_slug,
            title=text,
            description=desc,
            work_arrangement=arrangement,
            posted_date=posted_at,
            apply_url=apply_url,
            normalized_apply_url=norm_url,
            ats="lever",
            source="Lever",
            raw_evidence=raw,
            provenance_sources=["Lever"],
        )
