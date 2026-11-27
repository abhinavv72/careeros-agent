"""Greenhouse adapter — fetches and normalizes jobs from boards-api.greenhouse.io."""
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

_BASE = "https://boards-api.greenhouse.io/v1/boards"


def _plain_html(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "JobPilot/1.0 personal job search"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


class GreenhouseAdapter(BaseAdapter):
    """Greenhouse Jobs Board API."""

    def fetch_jobs(self, company_slug: str) -> Iterable[dict]:
        data = _fetch(f"{self.config.get('base_url', _BASE)}/{company_slug}/jobs?content=true")
        yield from data.get("jobs", [])

    def normalize(self, raw: dict, company_slug: str) -> NormalizedJob:
        raw_id = str(raw.get("id", ""))
        title = raw.get("title", "").strip()
        desc = _plain_html(raw.get("content", ""))
        raw_loc = raw.get("location", {}).get("name", "") if isinstance(raw.get("location"), dict) else str(raw.get("location") or "")
        apply_url = raw.get("absolute_url", "")
        norm_url = normalize_apply_url(apply_url)
        posted_at = raw.get("first_published_at", "") or raw.get("updated_at", "")
        
        # Work arrangement and geography separation
        arrangement = parse_work_arrangement(raw_loc, desc)

        return NormalizedJob(
            external_id=raw_id,
            employer=raw.get("_company", company_slug),
            title=title,
            description=desc,
            work_arrangement=arrangement,
            posted_date=posted_at,
            apply_url=apply_url,
            normalized_apply_url=norm_url,
            ats="greenhouse",
            source="Greenhouse",
            raw_evidence=raw,
            provenance_sources=["Greenhouse"],
        )
