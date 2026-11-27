"""Ashby adapter — fetches and normalizes jobs from Ashby posting API."""
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

_BASE = "https://api.ashbyhq.com/posting-api/job-board"


def _plain_html(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "JobPilot/1.0 personal job search"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


class AshbyAdapter(BaseAdapter):
    """Ashby public Job Board API."""

    def fetch_jobs(self, company_slug: str) -> Iterable[dict]:
        data = _fetch(f"{self.config.get('base_url', _BASE)}/{company_slug}?includeCompensation=true")
        yield from data.get("jobs", [])

    def normalize(self, raw: dict, company_slug: str) -> NormalizedJob:
        raw_id = str(raw.get("id", ""))
        title = raw.get("title", "").strip()
        desc = _plain_html(raw.get("descriptionHtml", "") or raw.get("descriptionPlain", ""))
        
        # Ashby often provides isRemote and address / location
        is_remote = raw.get("isRemote", False)
        loc_name = raw.get("location", "")
        if is_remote and "remote" not in loc_name.lower():
            loc_name = f"Remote, {loc_name}" if loc_name else "Remote"
            
        apply_url = raw.get("jobUrl", "") or raw.get("applyUrl", "")
        norm_url = normalize_apply_url(apply_url)
        posted_at = raw.get("publishedAt", "")
        
        arrangement = parse_work_arrangement(loc_name, desc)

        return NormalizedJob(
            external_id=raw_id,
            employer=raw.get("department", company_slug) or company_slug,
            title=title,
            description=desc,
            work_arrangement=arrangement,
            posted_date=posted_at,
            apply_url=apply_url,
            normalized_apply_url=norm_url,
            ats="ashby",
            source="Ashby",
            raw_evidence=raw,
            provenance_sources=["Ashby"],
        )
