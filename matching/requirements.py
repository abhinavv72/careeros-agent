"""Deterministic experience requirement parsing.

Separates core mandatory role experience from secondary/optional tool experience.
Prevents optional 1-year skill requirements from canceling a mandatory 5-year requirement.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from matching.models import ExperienceEvaluation

OPTIONAL_CONTEXT_WORDS = [
    "plus", "bonus", "nice to have", "preferred", "optional", "good to have",
    "desired", "advantageous", "not required", "helpful"
]


def evaluate_experience_requirements(
    job_text: str, candidate_years: float, max_allowed_years: float = 3.0
) -> ExperienceEvaluation:
    """Evaluate experience requirements from title and description.

    Rules:
    - Finds explicit mandatory role experience clauses (e.g. "5+ years of software development experience").
    - Detects if an experience mention is inside an optional/bonus context (e.g. "1+ years of AWS is a plus").
    - Optional tooling experience does NOT dilute or replace mandatory role experience.
    - Handles ranges: '2-4 years' -> within bounds if candidate has 3.0 years.
    - Handles alternatives: '3 years of C# or 5 years of Java' -> passes if candidate meets either.
    """
    clean_text = job_text or ""
    # Split text into sentences/clauses (do not split on hyphen so 2-4 is preserved)
    sentences = re.split(r"[.\n;•]", clean_text)

    mandatory_min: Optional[float] = None
    mandatory_max: Optional[float] = None
    optional_tools: Dict[str, float] = {}

    for raw_sentence in sentences:
        s = raw_sentence.strip().lower()
        if not s:
            continue

        is_optional = any(opt in s for opt in OPTIONAL_CONTEXT_WORDS)

        # 1. Alternative clauses: "3 years of C# or 5 years of Java"
        if " or " in s and "year" in s:
            alt_matches = re.findall(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:to|-)?\s*(\d+(?:\.\d+)?)?\s*(?:years?|yrs?)\s*(?:of)?\s*([a-z0-9#+.]+)?", s)
            if alt_matches:
                # If any alternative is within candidate range and not optional, consider it
                alts = [float(m[0]) for m in alt_matches]
                if is_optional:
                    for m in alt_matches:
                        tool = m[2].strip() or "general"
                        optional_tools[tool] = float(m[0])
                else:
                    # For alternatives, the minimum alternative is acceptable to meet the requirement
                    min_alt = min(alts)
                    if mandatory_min is None or min_alt > mandatory_min:
                        mandatory_min = min_alt
                continue

        # 2. Ranges: "2-4 years of experience" or "2 to 4 years"
        range_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:to|-)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s*(?:of)?\s*(?:experience)?", s)
        if range_match:
            r_min = float(range_match.group(1))
            r_max = float(range_match.group(2))
            if is_optional:
                optional_tools["tool_range"] = r_min
            else:
                if mandatory_min is None or r_min > mandatory_min:
                    mandatory_min = r_min
                if mandatory_max is None or r_max > mandatory_max:
                    mandatory_max = r_max
            continue

        # 3. Standard minimum experience: "5+ years of experience" or "at least 5 years"
        min_match = re.search(r"(?:at least|minimum of|require[sd]?)?\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\s*(?:of)?\s*([a-z0-9#+.\s]{0,30})\s*(?:experience)?", s)
        if min_match:
            yrs = float(min_match.group(1))
            subject = min_match.group(2).strip()
            
            # Check if this sentence is clearly about an optional tool or core role
            is_core_role = any(role_word in s for role_word in [
                "software", "engineering", "developer", "backend", "full stack",
                "development", "professional experience", "industry experience",
                "total experience", "overall experience", "work experience"
            ])
            
            if is_optional or (not is_core_role and subject and yrs <= 2.0 and mandatory_min and mandatory_min >= 4.0):
                optional_tools[subject or "tool"] = yrs
            else:
                # Mandatory experience
                if mandatory_min is None or yrs > mandatory_min:
                    mandatory_min = yrs
            continue

    # Decision logic based on parsed requirements
    if mandatory_min is not None:
        if mandatory_min > max_allowed_years:
            return ExperienceEvaluation(
                is_eligible=False,
                mandatory_min_years=mandatory_min,
                mandatory_max_years=mandatory_max,
                optional_tool_years=optional_tools,
                candidate_years=candidate_years,
                reason=f"Requires {mandatory_min} mandatory years of experience, exceeding candidate's target limit ({max_allowed_years} years)",
            )
        if candidate_years < mandatory_min:
            return ExperienceEvaluation(
                is_eligible=False,
                mandatory_min_years=mandatory_min,
                mandatory_max_years=mandatory_max,
                optional_tool_years=optional_tools,
                candidate_years=candidate_years,
                reason=f"Requires {mandatory_min} years experience; candidate has {candidate_years} years",
            )

    return ExperienceEvaluation(
        is_eligible=True,
        mandatory_min_years=mandatory_min,
        mandatory_max_years=mandatory_max,
        optional_tool_years=optional_tools,
        candidate_years=candidate_years,
        reason="Experience requirements satisfied",
    )
