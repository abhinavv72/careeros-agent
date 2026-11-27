"""Isolated unit and fixture tests for discovery package."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from discovery.models import NormalizedJob, WorkArrangement
from discovery.geography import parse_work_arrangement
from discovery.dedup import normalize_apply_url, deduplicate_jobs
from discovery.portal_registry import PortalRegistry
from discovery.adapters.greenhouse import GreenhouseAdapter
from discovery.adapters.lever import LeverAdapter
from discovery.adapters.ashby import AshbyAdapter


# ---------------------------------------------------------------------------
# 1. Geographic and Work Arrangement Tests
# ---------------------------------------------------------------------------

def test_foreign_remote_us_restricted():
    """US Remote job must be country restricted and NOT eligible for candidate in India."""
    res = parse_work_arrangement("Remote - US", "Looking for senior backend engineer in the United States.")
    assert res.arrangement_type == "remote"
    assert res.permitted_geography == "US"
    assert res.is_country_restricted is True
    assert res.is_india_eligible is False


def test_foreign_remote_croatia_restricted():
    """Croatia Remote job must be country restricted and NOT eligible for candidate in India."""
    res = parse_work_arrangement("Remote (Croatia)", "Candidates must be located in Croatia.")
    assert res.arrangement_type == "remote"
    assert res.permitted_geography == "Croatia"
    assert res.is_country_restricted is True
    assert res.is_india_eligible is False


def test_foreign_remote_described_in_body():
    """Bare remote location with country restriction stated only in description body."""
    res = parse_work_arrangement("Remote", "You must be located in United States to be considered for this role.")
    assert res.arrangement_type == "remote"
    assert res.permitted_geography == "US"
    assert res.is_country_restricted is True
    assert res.is_india_eligible is False


def test_india_remote():
    """India remote job must be marked as India-eligible."""
    res = parse_work_arrangement("India (Remote)", "Work from anywhere in India.")
    assert res.arrangement_type == "remote"
    assert res.permitted_geography == "India"
    assert res.is_country_restricted is True
    assert res.is_india_eligible is True


def test_india_onsite_or_hybrid():
    """Bengaluru onsite or hybrid role must be marked as India-eligible."""
    res = parse_work_arrangement("Bengaluru, India", "Hybrid model 2 days a week.")
    assert res.arrangement_type == "hybrid"
    assert res.permitted_geography == "India"
    assert res.is_india_eligible is True


def test_worldwide_remote():
    """Worldwide / Global remote roles must be marked eligible without country restriction."""
    for loc in ["Remote - Worldwide", "Global", "Anywhere", "Work from Anywhere"]:
        res = parse_work_arrangement(loc, "Join our fully distributed team globally.")
        assert res.arrangement_type == "remote"
        assert res.permitted_geography == "Worldwide"
        assert res.is_country_restricted is False
        assert res.is_india_eligible is True


def test_bare_remote_location_unknown():
    """Bare 'Remote' with no country specified must be marked is_india_eligible=None (review needed)."""
    res = parse_work_arrangement("Remote", "General engineering role details without geographic restrictions.")
    assert res.arrangement_type == "remote"
    assert res.permitted_geography == "Unknown"
    assert res.is_country_restricted is False
    assert res.is_india_eligible is None  # Unknown, must NOT assume eligible


def test_empty_location():
    """Missing or empty location must be marked unknown and review-needed."""
    res = parse_work_arrangement("", "")
    assert res.permitted_geography == "Unknown"
    assert res.is_india_eligible is None


# ---------------------------------------------------------------------------
# 2. URL Normalization and Deduplication Tests
# ---------------------------------------------------------------------------

def test_normalize_apply_url_strips_tracking():
    raw_url = "https://boards.greenhouse.io/company/jobs/12345?gh_src=custom&utm_source=linkedin&ref=tracker#apply"
    expected = "https://boards.greenhouse.io/company/jobs/12345"
    assert normalize_apply_url(raw_url) == expected


def test_deduplicate_jobs_by_url_and_fingerprint():
    job1 = NormalizedJob(
        external_id="101",
        employer="Acme Corp",
        title="Software Engineer",
        description="Build things",
        work_arrangement=parse_work_arrangement("Bengaluru, India"),
        posted_date="2026-10-10",
        apply_url="https://boards.greenhouse.io/acme/jobs/101?utm_source=indeed",
        normalized_apply_url="https://boards.greenhouse.io/acme/jobs/101",
        ats="greenhouse",
        source="Greenhouse",
    )
    # Same job with different tracking parameters
    job2 = NormalizedJob(
        external_id="101",
        employer="Acme Corp",
        title="Software Engineer",
        description="Build things",
        work_arrangement=parse_work_arrangement("Bengaluru, India"),
        posted_date="2026-10-10",
        apply_url="https://boards.greenhouse.io/acme/jobs/101?gh_src=linkedin",
        normalized_apply_url="https://boards.greenhouse.io/acme/jobs/101",
        ats="greenhouse",
        source="LinkedIn",
    )
    # Distinct job
    job3 = NormalizedJob(
        external_id="102",
        employer="Beta LLC",
        title="DevOps Engineer",
        description="Manage cloud",
        work_arrangement=parse_work_arrangement("Remote - India"),
        posted_date="2026-10-11",
        apply_url="https://jobs.lever.co/beta/102",
        normalized_apply_url="https://jobs.lever.co/beta/102",
        ats="lever",
        source="Lever",
    )

    result = deduplicate_jobs([job1, job2, job3])
    assert len(result.unique_jobs) == 2
    assert result.duplicates_removed == 1
    assert result.provenance_merged == 1
    # Check that provenance includes both Greenhouse and LinkedIn
    merged = result.unique_jobs[0]
    assert "Greenhouse" in merged.provenance_sources
    assert "LinkedIn" in merged.provenance_sources
    assert job2.apply_url in result.reasons


# ---------------------------------------------------------------------------
# 3. Adapter Normalization with Redacted Raw Fixtures
# ---------------------------------------------------------------------------

def test_greenhouse_adapter_normalization():
    raw_greenhouse_fixture = {
        "id": 987654,
        "title": "Senior Backend Developer",
        "content": "&lt;p&gt;Must have C# and .NET experience.&lt;/p&gt;",
        "location": {"name": "Bengaluru, India"},
        "absolute_url": "https://boards.greenhouse.io/testco/jobs/987654?gh_src=web",
        "first_published_at": "2026-10-12T10:00:00Z",
    }
    adapter = GreenhouseAdapter({"base_url": "https://boards-api.greenhouse.io/v1/boards"})
    norm = adapter.normalize(raw_greenhouse_fixture, "testco")

    assert norm.external_id == "987654"
    assert norm.employer == "testco"
    assert norm.title == "Senior Backend Developer"
    assert "Must have C# and .NET experience." in norm.description
    assert norm.work_arrangement.is_india_eligible is True
    assert norm.normalized_apply_url == "https://boards.greenhouse.io/testco/jobs/987654"
    assert norm.ats == "greenhouse"


def test_lever_adapter_normalization():
    raw_lever_fixture = {
        "id": "abc-123",
        "text": "Full Stack Engineer",
        "descriptionPlain": "Full stack role working across frontend and backend.",
        "categories": {"location": "Zagreb, Croatia", "commitment": "Full-time"},
        "workplaceType": "remote",
        "hostedUrl": "https://jobs.lever.co/croco/abc-123?lever-source=promo",
        "createdAt": 1728864000000,
    }
    adapter = LeverAdapter({"base_url": "https://api.lever.co/v0/postings"})
    norm = adapter.normalize(raw_lever_fixture, "croco")

    assert norm.external_id == "abc-123"
    assert norm.title == "Full Stack Engineer"
    assert norm.work_arrangement.permitted_geography == "Croatia"
    assert norm.work_arrangement.is_country_restricted is True
    assert norm.work_arrangement.is_india_eligible is False
    assert norm.normalized_apply_url == "https://jobs.lever.co/croco/abc-123"
    assert norm.ats == "lever"


def test_ashby_adapter_normalization():
    raw_ashby_fixture = {
        "id": "job-xyz",
        "title": "Platform Engineer",
        "descriptionPlain": "Distributed systems engineering globally.",
        "location": "Worldwide",
        "isRemote": True,
        "jobUrl": "https://jobs.ashbyhq.com/globalco/job-xyz?utm_source=ref",
        "publishedAt": "2026-10-13T12:00:00Z",
    }
    adapter = AshbyAdapter({"base_url": "https://api.ashbyhq.com/posting-api/job-board"})
    norm = adapter.normalize(raw_ashby_fixture, "globalco")

    assert norm.external_id == "job-xyz"
    assert norm.title == "Platform Engineer"
    assert norm.work_arrangement.permitted_geography == "Worldwide"
    assert norm.work_arrangement.is_india_eligible is True
    assert norm.normalized_apply_url == "https://jobs.ashbyhq.com/globalco/job-xyz"
    assert norm.ats == "ashby"


def test_adapter_handles_malformed_raw_payload():
    """Adapter must tolerate missing fields without crashing."""
    adapter = GreenhouseAdapter({"base_url": ""})
    norm = adapter.normalize({}, "unknown_co")
    assert norm.external_id == ""
    assert norm.title == ""
    assert norm.work_arrangement.permitted_geography == "Unknown"
    assert norm.work_arrangement.is_india_eligible is None
