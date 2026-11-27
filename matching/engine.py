"""Deterministic matching engine for JobPilot.

Pure functions that evaluate job eligibility, compute explainable score breakdowns,
and recommend optimal resumes without mutating databases, configs, or application states.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from discovery.models import NormalizedJob, WorkArrangement
from matching.models import MatchDecision, ScoreBreakdown
from matching.requirements import evaluate_experience_requirements

AVAILABLE_RESUMES = ["backend", "fullstack", "platform-sre", "security", "generic"]


def _salary_lpa(text: str) -> Optional[float]:
    """Read explicit INR LPA ranges; leave other currencies and vague claims unknown."""
    ranges = re.findall(r"(?:₹\s*)?(\d+(?:\.\d+)?)\s*(?:-|to|–|—)\s*(\d+(?:\.\d+)?)\s*(?:lpa|lakhs?\s*(?:per\s+annum|/\s*annum|p\.a\.)?)", text or "", re.I)
    if ranges:
        return float(ranges[-1][1])
    singles = re.findall(r"(?:₹\s*)?(\d+(?:\.\d+)?)\s*(?:lpa|lakhs?\s*(?:per\s+annum|/\s*annum|p\.a\.))", text or "", re.I)
    return float(singles[-1]) if singles else None


def select_resume_version(
    job_title: str, job_description: str, available_resumes: Optional[List[str]] = None
) -> str:
    """Deterministic resume selector based on role title and description evidence.

    Title takes precedence over description keywords.
    """
    avail = available_resumes or AVAILABLE_RESUMES
    t_lower = (job_title or "").lower()
    d_lower = (job_description or "").lower()
    combined = f"{t_lower} {d_lower}"

    # 1. Platform / SRE / DevOps
    if any(k in t_lower for k in ["sre", "devops", "platform", "infrastructure", "reliability"]):
        if "platform-sre" in avail:
            return "platform-sre"

    # 2. Security
    if any(k in t_lower for k in ["security", "appsec", "cybersecurity", "infosec"]):
        if "security" in avail:
            return "security"

    # 3. Full Stack
    if any(k in t_lower for k in ["full stack", "fullstack", "frontend", "angular", "react"]):
        if "fullstack" in avail:
            return "fullstack"

    # 4. Backend / .NET
    if any(k in t_lower for k in ["backend", ".net", "c#", "dotnet", "server"]):
        if "backend" in avail:
            return "backend"

    # Secondary check on description body
    if any(k in combined for k in ["platform", "kubernetes", "terraform", "sre"]) and "platform-sre" in avail:
        return "platform-sre"
    if any(k in combined for k in ["security", "owasp", "vulnerability"]) and "security" in avail:
        return "security"
    if any(k in combined for k in ["full stack", "fullstack", "angular"]) and "fullstack" in avail:
        return "fullstack"

    return "backend" if "backend" in avail else "generic"


def match_job(
    job: NormalizedJob | Dict[str, Any],
    profile: Dict[str, Any],
    skills_config: Dict[str, Any],
    eligibility_rules: Dict[str, Any],
) -> MatchDecision:
    """Evaluate job match, eligibility, explainable score breakdown, and resume.

    Pure function: deterministic, no database queries, no side effects.
    """
    # Extract fields from NormalizedJob or dict
    if isinstance(job, NormalizedJob):
        title = job.title
        desc = job.description
        work_arr = job.work_arrangement
        ats = job.ats
    else:
        title = job.get("title", "")
        desc = job.get("description", "")
        raw_loc = job.get("location", "")
        # If work_arrangement not provided, parse it
        from discovery.geography import parse_work_arrangement
        work_arr = parse_work_arrangement(raw_loc, desc)
        ats = job.get("ats", "unknown")

    title_lower = title.lower()
    combined_text = f"{title_lower} {desc.lower()}"

    # -----------------------------------------------------------------------
    # 1. Closed or Stale Job Detection
    # -----------------------------------------------------------------------
    if re.search(r"\b(job closed|no longer accepting applications|position has been filled|expired)\b", combined_text):
        return MatchDecision(
            eligible=False,
            eligibility_reason="Job is closed or no longer accepting applications",
            decision_code="CLOSED_ROLE",
            score=0.0,
            score_breakdown=ScoreBreakdown(0, 0, 0, 0, 0, 0, [], [], [], ["Closed role"]),
            recommended_resume="generic",
            explanation="Closed role detected in listing text",
        )

    # -----------------------------------------------------------------------
    # 2. Work Authorization / Geographic Eligibility
    # -----------------------------------------------------------------------
    loc_score = 0.0
    if work_arr.is_india_eligible is False:
        return MatchDecision(
            eligible=False,
            eligibility_reason=f"Geographically restricted to {work_arr.permitted_geography}; candidate in India ineligible",
            decision_code="LOCATION_BLOCKED",
            score=0.0,
            score_breakdown=ScoreBreakdown(0, 0, 0, 0, 0, 0, [], [], [], [f"Restricted to {work_arr.permitted_geography}"]),
            recommended_resume="generic",
            explanation=f"Role requires residence in {work_arr.permitted_geography}",
        )
    elif work_arr.is_india_eligible is True:
        loc_score = 15.0
    else:
        # Unknown location - requires manual review, do not assume eligible
        loc_score = 5.0

    # -----------------------------------------------------------------------
    # 3. Role Seniority & Title Fit
    # -----------------------------------------------------------------------
    candidate_exp = float(profile.get("experience", {}).get("years", 3.0))
    seniority_match = re.search(r"\b(senior|sr\.?|staff|director|vice president|vp|principal|architect|head of|manager|lead)\b", title_lower)
    seniority_exp = evaluate_experience_requirements(combined_text, candidate_exp, float(eligibility_rules.get("maxExperienceYears", 3.0))) if seniority_match else None
    if seniority_match and (seniority_exp.mandatory_min_years is None or seniority_exp.mandatory_min_years > candidate_exp):
        return MatchDecision(
            eligible=False,
            eligibility_reason="Seniority level (Senior/Lead/Staff/Director) exceeds candidate target profile",
            decision_code="ROLE_MISMATCH",
            score=0.0,
            score_breakdown=ScoreBreakdown(0, 0, 0, 0, 0, 0, [], [], [], ["Seniority mismatch"]),
            recommended_resume="generic",
            explanation="Senior role excluded based on target title preferences",
        )

    # Keep adjacent job families out of a software engineering target profile.
    # These titles often share words such as "developer" or "engineer" with
    # target roles but are different jobs in practice.
    if re.search(r"\b(product|project|program|technical account|customer success|developer support|technical support|customer support|solutions|sales|account executive|recruiter)\b", title_lower):
        return MatchDecision(
            eligible=False,
            eligibility_reason="Role family does not match the candidate's software engineering preferences",
            decision_code="ROLE_MISMATCH",
            score=0.0,
            score_breakdown=ScoreBreakdown(0, 0, 0, 0, 0, 0, [], [], [], ["Role family mismatch"]),
            recommended_resume="generic",
            explanation="Role family excluded based on the candidate's target preferences",
        )

    # Internship check
    if not eligibility_rules.get("allowInternships", False) and re.search(r"\bintern(ship)?\b", title_lower):
        return MatchDecision(
            eligible=False,
            eligibility_reason="Internship roles excluded by eligibility policy",
            decision_code="ROLE_MISMATCH",
            score=0.0,
            score_breakdown=ScoreBreakdown(0, 0, 0, 0, 0, 0, [], [], [], ["Internship excluded"]),
            recommended_resume="generic",
            explanation="Internship role excluded",
        )

    # -----------------------------------------------------------------------
    # 4. Experience Requirements
    # -----------------------------------------------------------------------
    max_exp = float(eligibility_rules.get("maxExperienceYears", 3.0))
    exp_eval = evaluate_experience_requirements(combined_text, candidate_exp, max_exp)
    if not exp_eval.is_eligible:
        return MatchDecision(
            eligible=False,
            eligibility_reason=exp_eval.reason,
            decision_code="EXPERIENCE_EXCEEDED",
            score=0.0,
            score_breakdown=ScoreBreakdown(0, 0, 0, 0, 0, 0, [], [], [], [exp_eval.reason]),
            recommended_resume="generic",
            explanation=exp_eval.reason,
        )

    # Experience fit score: full score if experience requirements are explicitly matched
    exp_score = 15.0 if exp_eval.mandatory_min_years is not None else 10.0

    # -----------------------------------------------------------------------
    # 5. Role Target Matching (0-30 pts)
    # -----------------------------------------------------------------------
    pref_roles = [r.lower() for r in profile.get("preferences", {}).get("roles", [])]
    matched_role_ratio = 0.0
    for target in pref_roles:
        target_tokens = set(target.split())
        title_tokens = set(re.findall(r"[a-z0-9#+.]+", title_lower))
        if target_tokens and target_tokens.issubset(title_tokens):
            matched_role_ratio = 1.0
            break
        overlap = len(target_tokens & title_tokens) / max(len(target_tokens), 1)
        if overlap > matched_role_ratio:
            matched_role_ratio = overlap
    role_score = round(matched_role_ratio * 30.0, 1)

    # -----------------------------------------------------------------------
    # 6. Skills Evidence Scoring (Required: 0-25 pts, Optional: 0-15 pts)
    # -----------------------------------------------------------------------
    weighted_skills = skills_config.get("weighted", {})
    req_skills = [k for k, v in weighted_skills.items() if v >= 4]
    opt_skills = [k for k, v in weighted_skills.items() if v < 4]

    matched_req: List[str] = []
    missing_req: List[str] = []
    matched_opt: List[str] = []
    uncertainties: List[str] = []

    for s in req_skills:
        s_esc = re.escape(s.lower())
        pattern = r"(?<![a-z0-9])" + s_esc + r"(?![a-z0-9])"
        if re.search(pattern, combined_text):
            matched_req.append(s)
        else:
            missing_req.append(s)

    for s in opt_skills:
        s_esc = re.escape(s.lower())
        pattern = r"(?<![a-z0-9])" + s_esc + r"(?![a-z0-9])"
        if re.search(pattern, combined_text):
            matched_opt.append(s)

    req_score = round((len(matched_req) / max(len(req_skills), 1)) * 25.0, 1)
    opt_score = round((len(matched_opt) / max(len(opt_skills), 1)) * 15.0, 1)

    if missing_req:
        uncertainties.append(f"Missing candidate core skills: {', '.join(missing_req[:3])}")
    if work_arr.is_india_eligible is None:
        uncertainties.append("Location is ambiguous or unspecified; human review needed")

    # -----------------------------------------------------------------------
    # 7. Total Score & Resume Selection
    # -----------------------------------------------------------------------
    salary_target = profile.get("preferences", {}).get("minimumSalaryLpa")
    salary_posted = _salary_lpa(desc)
    salary_score = None
    salary_note = "Salary fit is unknown because the posting does not state a parseable INR range"
    if salary_target is not None and salary_posted is not None and float(salary_target) > 0:
        salary_score = round(min(10.0, 10.0 * salary_posted / float(salary_target)), 1)
        salary_note = f"Posting states up to ₹{salary_posted:g} LPA; saved target is ₹{float(salary_target):g} LPA"
        role_score, req_score, opt_score, loc_score, exp_score = (round(value * .9, 1) for value in (role_score, req_score, opt_score, loc_score, exp_score))
        total = round(min(100.0, role_score + req_score + opt_score + loc_score + exp_score + salary_score), 1)
    else:
        total = round(min(100.0, role_score + req_score + opt_score + loc_score + exp_score), 1)
    resume = select_resume_version(title, desc)

    is_eligible = True
    decision_code = "ELIGIBLE"
    reason = "Meets experience, location, and role criteria"

    if work_arr.is_india_eligible is None:
        decision_code = "REVIEW_NEEDED"
        reason = "Location eligibility uncertain; manual verification required"

    breakdown = ScoreBreakdown(
        role_score=role_score,
        required_skills_score=req_score,
        optional_skills_score=opt_score,
        location_score=loc_score,
        experience_fit_score=exp_score,
        total_score=total,
        matched_required_skills=matched_req,
        missing_required_skills=missing_req,
        matched_optional_skills=matched_opt,
        uncertainties=uncertainties,
        salary_score=salary_score,
        salary_note=salary_note,
    )

    explanation = f"Score {total}: Role ({role_score}), Core skills ({req_score}), Bonus skills ({opt_score}), Location ({loc_score}), Experience ({exp_score}); Salary ({salary_score if salary_score is not None else 'unknown'}). {salary_note}. Recommended resume: {resume}."

    return MatchDecision(
        eligible=is_eligible,
        eligibility_reason=reason,
        decision_code=decision_code,
        score=total,
        score_breakdown=breakdown,
        recommended_resume=resume,
        explanation=explanation,
    )
