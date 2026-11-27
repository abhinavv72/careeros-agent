"""Isolated unit tests for matching package."""
from __future__ import annotations

import pytest

from discovery.models import NormalizedJob, WorkArrangement
from matching.models import MatchDecision, ExperienceEvaluation
from matching.requirements import evaluate_experience_requirements
from matching.engine import match_job, select_resume_version

PROFILE = {
    "personal": {"firstName": "Sample", "lastName": "Candidate"},
    "experience": {"years": 3.0},
    "preferences": {
        "roles": ["Software Engineer", "Backend Developer", "Full Stack Developer", "Platform Engineer"],
        "locations": ["Bengaluru", "Hyderabad", "Remote", "India"],
    },
}

SKILLS = {
    "weighted": {
        "C#": 5,
        ".NET": 5,
        "SQL": 4,
        "REST API": 4,
        "Angular": 3,
        "Azure": 3,
        "Docker": 2,
        "Kubernetes": 1,
    }
}

RULES = {
    "maxExperienceYears": 3.0,
    "allowInternships": False,
    "minimumScore": 50,
}


def test_mandatory_five_years_not_canceled_by_optional_one_year_tool():
    """Mandatory 5+ years requirement must NOT be canceled by optional 1-year skill requirement."""
    text = (
        "We are looking for a Software Engineer with at least 5+ years of software development experience. "
        "1+ year of Docker or Kubernetes experience is a bonus."
    )
    res = evaluate_experience_requirements(text, candidate_years=3.0, max_allowed_years=3.0)
    assert res.is_eligible is False
    assert res.mandatory_min_years == 5.0
    assert "exceeding candidate's target limit" in res.reason


def test_experience_within_candidate_range():
    """2-4 years experience range fits candidate with 3.0 years."""
    text = "Requires 2-4 years of experience in backend development."
    res = evaluate_experience_requirements(text, candidate_years=3.0, max_allowed_years=4.0)
    assert res.is_eligible is True
    assert res.mandatory_min_years == 2.0
    assert res.mandatory_max_years == 4.0


def test_salary_fit_uses_explicit_inr_lpa_and_saved_target():
    profile = {**PROFILE, "preferences": {**PROFILE["preferences"], "minimumSalaryLpa": 15}}
    decision = match_job(
        {"title": "Backend Engineer", "description": "C# .NET SQL REST API. Compensation: ₹12–18 LPA.", "location": "Hyderabad, India", "ats": "greenhouse"},
        profile, SKILLS, RULES,
    )
    assert decision.score_breakdown.salary_score == 10
    assert "target is ₹15 LPA" in decision.score_breakdown.salary_note
    assert decision.score <= 100


def test_salary_stays_unknown_when_posting_has_no_explicit_range():
    profile = {**PROFILE, "preferences": {**PROFILE["preferences"], "minimumSalaryLpa": 15}}
    decision = match_job(
        {"title": "Backend Engineer", "description": "C# .NET SQL REST API.", "location": "Hyderabad, India", "ats": "greenhouse"},
        profile, SKILLS, RULES,
    )
    assert decision.score_breakdown.salary_score is None
    assert "unknown" in decision.score_breakdown.salary_note


def test_senior_title_is_allowed_when_explicit_requirement_matches_candidate():
    decision = match_job(
        {"title": "Sr. Software Engineer", "description": "3+ years of software development experience building backend APIs", "location": "Hyderabad, India", "ats": "greenhouse"},
        PROFILE,
        SKILLS,
        RULES,
    )
    assert decision.eligible is True


def test_senior_title_without_matching_experience_requirement_stays_excluded():
    decision = match_job(
        {"title": "Senior Software Engineer", "description": "Build backend APIs", "location": "Hyderabad, India", "ats": "greenhouse"},
        PROFILE,
        SKILLS,
        RULES,
    )
    assert decision.eligible is False
    assert decision.decision_code == "ROLE_MISMATCH"


def test_alternative_experience_requirements():
    """Alternative requirements: 3 years C# or 5 years Java matches candidate with 3 years C#."""
    text = "Requires 3 years of C# or 5 years of Java."
    res = evaluate_experience_requirements(text, candidate_years=3.0, max_allowed_years=3.0)
    assert res.is_eligible is True


def test_foreign_remote_job_disqualified_with_location_code():
    """US-only remote job must be disqualified with LOCATION_BLOCKED decision code."""
    job = NormalizedJob(
        external_id="101",
        employer="USCorp",
        title="Software Engineer",
        description="Looking for C# and .NET engineer.",
        work_arrangement=WorkArrangement(
            arrangement_type="remote",
            permitted_geography="US",
            is_country_restricted=True,
            is_india_eligible=False,
            raw_location="Remote - US",
        ),
        posted_date="2026-10-14",
        apply_url="https://jobs.example/101",
        normalized_apply_url="https://jobs.example/101",
        ats="greenhouse",
        source="Greenhouse",
    )
    decision = match_job(job, PROFILE, SKILLS, RULES)
    assert decision.eligible is False
    assert decision.decision_code == "LOCATION_BLOCKED"
    assert "Geographically restricted to US" in decision.eligibility_reason


def test_india_remote_job_eligible_with_score_breakdown():
    """India remote job with matching stack receives high score and detailed breakdown."""
    job = NormalizedJob(
        external_id="102",
        employer="IndiaTech",
        title="Backend Developer",
        description="Core stack is C#, .NET, SQL and REST API. Azure is good to have.",
        work_arrangement=WorkArrangement(
            arrangement_type="remote",
            permitted_geography="India",
            is_country_restricted=True,
            is_india_eligible=True,
            raw_location="Remote - India",
        ),
        posted_date="2026-10-14",
        apply_url="https://jobs.example/102",
        normalized_apply_url="https://jobs.example/102",
        ats="lever",
        source="Lever",
    )
    decision = match_job(job, PROFILE, SKILLS, RULES)
    assert decision.eligible is True
    assert decision.decision_code == "ELIGIBLE"
    assert decision.score >= 70.0
    assert "C#" in decision.score_breakdown.matched_required_skills
    assert ".NET" in decision.score_breakdown.matched_required_skills
    assert decision.recommended_resume == "backend"


def test_resume_selection_specialization():
    """Deterministic selection of platform, security, fullstack, and backend resumes."""
    assert select_resume_version("DevOps / SRE Engineer", "Kubernetes cluster management") == "platform-sre"
    assert select_resume_version("Application Security Analyst", "Vulnerability management and OWASP") == "security"
    assert select_resume_version("Full Stack Developer", "Angular frontend and C# backend") == "fullstack"
    assert select_resume_version("C# Backend Engineer", "High throughput APIs") == "backend"


def test_stale_or_closed_role_disqualified():
    """Closed role text produces CLOSED_ROLE decision."""
    job = NormalizedJob(
        external_id="103",
        employer="ClosedCo",
        title="Software Engineer",
        description="This position has been filled. No longer accepting applications.",
        work_arrangement=WorkArrangement(
            arrangement_type="remote",
            permitted_geography="Worldwide",
            is_country_restricted=False,
            is_india_eligible=True,
            raw_location="Worldwide",
        ),
        posted_date="2026-10-10",
        apply_url="https://jobs.example/103",
        normalized_apply_url="https://jobs.example/103",
        ats="greenhouse",
        source="Greenhouse",
    )
    decision = match_job(job, PROFILE, SKILLS, RULES)
    assert decision.eligible is False
    assert decision.decision_code == "CLOSED_ROLE"


@pytest.mark.parametrize("title", [
    "Lead, Platform Engineering",
    "Manager II, Software Engineering",
    "Product Manager, Developer Platform",
    "Developer Support Engineer",
])
def test_adjacent_or_senior_roles_do_not_match_software_engineering_targets(title):
    job = NormalizedJob(
        external_id="role-mismatch",
        employer="Example",
        title=title,
        description="C# .NET SQL backend platform",
        work_arrangement=WorkArrangement("onsite", "India", True, True, "Bengaluru, India"),
        posted_date="",
        apply_url="https://jobs.example/role-mismatch",
        normalized_apply_url="https://jobs.example/role-mismatch",
        ats="greenhouse",
        source="Greenhouse",
    )
    decision = match_job(job, PROFILE, SKILLS, RULES)
    assert decision.eligible is False
    assert decision.decision_code == "ROLE_MISMATCH"
