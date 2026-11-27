"""Stub adapters for SmartRecruiters, BambooHR, and Workable."""
from __future__ import annotations

from typing import Any, Dict, Iterable

from discovery.base import BaseAdapter
from discovery.dedup import normalize_apply_url
from discovery.geography import parse_work_arrangement
from discovery.models import NormalizedJob


class SmartRecruitersAdapter(BaseAdapter):
    def fetch_jobs(self, company_slug: str) -> Iterable[dict]:
        return []

    def normalize(self, raw: dict, company_slug: str) -> NormalizedJob:
        return NormalizedJob(
            external_id=str(raw.get("id", "")),
            employer=company_slug,
            title=raw.get("name", ""),
            description="",
            work_arrangement=parse_work_arrangement(raw.get("location", {}).get("city", "")),
            posted_date="",
            apply_url=raw.get("applyUrl", ""),
            normalized_apply_url=normalize_apply_url(raw.get("applyUrl", "")),
            ats="smartrecruiters",
            source="SmartRecruiters",
            raw_evidence=raw,
            provenance_sources=["SmartRecruiters"],
        )


class BambooHRAdapter(BaseAdapter):
    def fetch_jobs(self, company_slug: str) -> Iterable[dict]:
        return []

    def normalize(self, raw: dict, company_slug: str) -> NormalizedJob:
        return NormalizedJob(
            external_id=str(raw.get("id", "")),
            employer=company_slug,
            title=raw.get("jobTitle", ""),
            description="",
            work_arrangement=parse_work_arrangement(raw.get("location", "")),
            posted_date="",
            apply_url=raw.get("url", ""),
            normalized_apply_url=normalize_apply_url(raw.get("url", "")),
            ats="bamboohr",
            source="BambooHR",
            raw_evidence=raw,
            provenance_sources=["BambooHR"],
        )


class WorkableAdapter(BaseAdapter):
    def fetch_jobs(self, company_slug: str) -> Iterable[dict]:
        return []

    def normalize(self, raw: dict, company_slug: str) -> NormalizedJob:
        return NormalizedJob(
            external_id=str(raw.get("shortcode", "")),
            employer=company_slug,
            title=raw.get("title", ""),
            description="",
            work_arrangement=parse_work_arrangement(raw.get("city", "")),
            posted_date="",
            apply_url=raw.get("url", ""),
            normalized_apply_url=normalize_apply_url(raw.get("url", "")),
            ats="workable",
            source="Workable",
            raw_evidence=raw,
            provenance_sources=["Workable"],
        )
