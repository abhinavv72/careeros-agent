"""Canonical data models for job discovery and source normalization."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class WorkArrangement:
    """Work arrangement and geographic residence constraints."""

    arrangement_type: str  # "remote" | "hybrid" | "onsite" | "unknown"
    permitted_geography: str  # e.g., "India", "US", "Worldwide", "Croatia", "Unknown"
    is_country_restricted: bool  # True if restricted to specific countries/regions
    is_india_eligible: Optional[bool]  # True if eligible in India, False if restricted/ineligible, None if unknown/review-needed
    raw_location: str  # The original unparsed location string


@dataclass(frozen=True)
class NormalizedJob:
    """Stable, canonical representation of a discovered job posting."""

    external_id: str
    employer: str
    title: str
    description: str
    work_arrangement: WorkArrangement
    posted_date: str  # ISO-8601 string or empty if unknown
    apply_url: str
    normalized_apply_url: str
    ats: str  # e.g., "greenhouse", "lever", "ashby", "bamboohr", "workable", "smartrecruiters", "unknown"
    source: str  # e.g., "Greenhouse", "Lever", "Ashby", "Indeed", "Naukri"
    raw_evidence: Dict[str, Any] = field(default_factory=dict)
    provenance_sources: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class DeduplicationResult:
    """Result of batch job deduplication."""

    unique_jobs: List[NormalizedJob]
    duplicates_removed: int
    provenance_merged: int
    reasons: Dict[str, str]  # normalized_apply_url or identifier -> reason
