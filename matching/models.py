"""Data models and evaluation results for deterministic job matching."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ExperienceEvaluation:
    """Detailed evaluation of job experience requirements."""

    is_eligible: bool
    mandatory_min_years: Optional[float]
    mandatory_max_years: Optional[float]
    optional_tool_years: Dict[str, float]
    candidate_years: float
    reason: str


@dataclass(frozen=True)
class ScoreBreakdown:
    """Explainable, evidence-based score breakdown."""

    role_score: float  # 0 to 30
    required_skills_score: float  # 0 to 25
    optional_skills_score: float  # 0 to 15
    location_score: float  # 0 to 15
    experience_fit_score: float  # 0 to 15
    total_score: float  # 0 to 100
    matched_required_skills: List[str]
    missing_required_skills: List[str]
    matched_optional_skills: List[str]
    uncertainties: List[str]
    salary_score: Optional[float] = None
    salary_note: str = "Salary fit is unknown because the posting does not state a parseable INR range"


@dataclass(frozen=True)
class MatchDecision:
    """Comprehensive matching decision with reason codes and explanations."""

    eligible: bool
    eligibility_reason: str
    decision_code: str  # "ELIGIBLE" | "LOCATION_BLOCKED" | "EXPERIENCE_EXCEEDED" | "ROLE_MISMATCH" | "REVIEW_NEEDED" | "CLOSED_ROLE"
    score: float
    score_breakdown: ScoreBreakdown
    recommended_resume: str  # e.g., "backend", "fullstack", "platform-sre", "security", "generic"
    explanation: str
