"""
UI fixtures for dashboard acceptance tests.
Provides standard mocked responses for all required states:
- ready
- unsupported_adapter
- missing_answer
- missing_resume
- retry_not_due
- challenge_waiting
- stale_queue_entry
- manual_review
- confirmed_applied
- empty_data
- api_failure
"""

def make_job(
    job_id: int = 1,
    company: str = "Acme Corp",
    title: str = "Senior Python Engineer",
    location: str = "Remote, US",
    ats: str = "greenhouse",
    status: str = "Queued",
    score: int = 85,
    apply_url: str = "https://boards.greenhouse.io/acme/jobs/1",
    can_submit: bool = True,
    reason_code: str = "ready",
    message: str = "Application is ready for automated submission.",
    recovery_action: str = None,
    recovery_category: str = None,
    attempts: int = 0,
    next_attempt_at: str = None,
    submitted: bool = False,
    application_mode: str = None,
    resume: str = "backend",
):
    job = {
        "id": job_id,
        "company": company,
        "title": title,
        "location": location,
        "ats": ats,
        "status": status,
        "score": score,
        "apply_url": apply_url,
        "source_url": apply_url,
        "can_submit": can_submit,
        "readiness": {
            "ready": can_submit,
            "reason_code": reason_code,
            "message": message,
        },
        "recovery": {
            "action": recovery_action,
            "category": recovery_category,
        } if recovery_action else None,
        "attempts": attempts,
        "next_attempt_at": next_attempt_at,
        "submitted": submitted,
        "application_mode": application_mode,
        "resume": resume,
        "resume_version": resume,
    }
    return job

FIXTURES = {
    "ready": {
        "jobs": [
            make_job(
                job_id=101,
                company="ReadyTech",
                title="Staff Backend Architect",
                location="Remote",
                ats="greenhouse",
                status="Queued",
                can_submit=True,
                reason_code="ready",
                message="Ready to apply",
                recovery_action="Queue is ready for submission",
            )
        ],
        "queue": [
            {
                "id": 101,
                "job_id": 101,
                "company": "ReadyTech",
                "title": "Staff Backend Architect",
                "location": "Remote",
                "score": 90,
                "resume_version": "backend",
                "status": "Queued",
                "job_status": "Queued",
                "can_submit": True,
                "readiness": {"ready": True, "reason_code": "ready", "message": "Ready to apply"},
            }
        ],
        "preview": {
            "count": 1,
            "items": [
                {
                    "job_id": 101,
                    "company": "ReadyTech",
                    "title": "Staff Backend Architect",
                    "location": "Remote",
                    "resume": "backend",
                    "score": 90,
                    "ats": "greenhouse",
                }
            ]
        }
    },
    "unsupported_adapter": {
        "jobs": [
            make_job(
                job_id=102,
                company="LegacySystems",
                title="Systems Engineer",
                location="Austin, TX",
                ats="workday",
                status="Queued",
                can_submit=False,
                reason_code="unsupported_adapter",
                message="ATS 'workday' does not support one-click submission. Apply manually.",
                recovery_action="Apply manually on the employer portal",
                recovery_category="manual",
            )
        ],
        "queue": [
            {
                "id": 102,
                "job_id": 102,
                "company": "LegacySystems",
                "title": "Systems Engineer",
                "location": "Austin, TX",
                "score": 75,
                "resume_version": "generic",
                "status": "Queued",
                "job_status": "Queued",
                "can_submit": False,
                "readiness": {"ready": False, "reason_code": "unsupported_adapter", "message": "ATS not supported for one-click"},
            }
        ]
    },
    "missing_answer": {
        "jobs": [
            make_job(
                job_id=103,
                company="QuestionableAI",
                title="ML Ops Lead",
                location="San Francisco, CA",
                ats="lever",
                status="Queued",
                can_submit=False,
                reason_code="missing_answer",
                message="Form contains 2 unanswered custom questions.",
                recovery_action="Answer custom employer questions",
                recovery_category="input",
            )
        ],
        "queue": [
            {
                "id": 103,
                "job_id": 103,
                "company": "QuestionableAI",
                "title": "ML Ops Lead",
                "location": "San Francisco, CA",
                "score": 88,
                "resume_version": "backend",
                "status": "Queued",
                "job_status": "Queued",
                "can_submit": False,
                "readiness": {"ready": False, "reason_code": "missing_answer", "message": "Missing answers for 2 questions"},
            }
        ]
    },
    "missing_resume": {
        "jobs": [
            make_job(
                job_id=104,
                company="ResumeLess Inc",
                title="Fullstack Developer",
                location="Remote",
                ats="greenhouse",
                status="Queued",
                can_submit=False,
                reason_code="resume_missing",
                message="Configured resume variant is missing or not assigned.",
                recovery_action="Assign a tailored resume in Application Queue",
                recovery_category="input",
            )
        ],
        "queue": [
            {
                "id": 104,
                "job_id": 104,
                "company": "ResumeLess Inc",
                "title": "Fullstack Developer",
                "location": "Remote",
                "score": 82,
                "resume_version": "",
                "status": "Queued",
                "job_status": "Queued",
                "can_submit": False,
                "readiness": {"ready": False, "reason_code": "resume_missing", "message": "Resume missing"},
            }
        ]
    },
    "retry_not_due": {
        "jobs": [
            make_job(
                job_id=105,
                company="BackoffCorp",
                title="Site Reliability Engineer",
                location="Seattle, WA",
                ats="greenhouse",
                status="FailedTemporary",
                can_submit=False,
                reason_code="retry_not_due",
                message="Submission retry is scheduled for 2026-10-18T12:00:00Z.",
                recovery_action="Wait for scheduled retry backoff",
                recovery_category="retry",
                attempts=2,
                next_attempt_at="2026-10-18T12:00:00Z",
            )
        ],
        "queue": []
    },
    "challenge_waiting": {
        "jobs": [
            make_job(
                job_id=106,
                company="CaptchaDefense",
                title="Security Analyst",
                location="New York, NY",
                ats="greenhouse",
                status="Queued",
                can_submit=False,
                reason_code="challenge_waiting",
                message="Cloudflare Turnstile challenge waiting for user interaction.",
                recovery_action="Complete verification check in browser",
                recovery_category="manual",
            )
        ],
        "queue": [],
        "captcha": {
            "status": "WaitingForUser",
            "company": "CaptchaDefense",
            "job_id": 106,
            "phase": "application",
            "challenge_id": "ch_xyz123",
        }
    },
    "stale_queue_entry": {
        "jobs": [
            make_job(
                job_id=107,
                company="StaleFlow",
                title="Lead Architect",
                location="Remote",
                ats="greenhouse",
                status="Applied",
                can_submit=False,
                reason_code="stale_queue_entry",
                message="Queue record out of sync with tracker status.",
                submitted=True,
            )
        ],
        "queue": [
            {
                "id": 107,
                "job_id": 107,
                "company": "StaleFlow",
                "title": "Lead Architect",
                "location": "Remote",
                "score": 92,
                "resume_version": "backend",
                "status": "Queued",
                "job_status": "Applied",
                "can_submit": False,
                "readiness": {"ready": False, "reason_code": "stale_queue_entry", "message": "Stale entry in queue"},
            }
        ]
    },
    "manual_review": {
        "jobs": [
            make_job(
                job_id=108,
                company="HumanCheck Ltd",
                title="Risk Officer",
                location="Chicago, IL",
                ats="lever",
                status="ManualReview",
                can_submit=False,
                reason_code="manual_review_needed",
                message="Salary expectations or disclaimers require candidate review.",
                recovery_action="Review application before manual submission",
                recovery_category="manual",
            )
        ],
        "queue": []
    },
    "confirmed_applied": {
        "jobs": [
            make_job(
                job_id=109,
                company="SuccessTech",
                title="Platform Engineer",
                location="Remote",
                ats="greenhouse",
                status="Applied",
                can_submit=False,
                reason_code="already_applied",
                message="Submitted and confirmed by backend.",
                submitted=True,
                application_mode="ONE_CLICK_AUTOMATED",
            )
        ],
        "queue": []
    },
    "empty_data": {
        "jobs": [],
        "queue": [],
        "preview": {"count": 0, "items": []}
    },
    "empty": {
        "jobs": [],
        "queue": [],
        "preview": {"limit": 5, "count": 0, "items": []},
        "counts": [],
        "runs": [],
        "active": None,
    },
    "running": {
        "active": "run-live-9001",
        "runs": [
            {
                "id": "run-live-9001",
                "action": "apply",
                "status": "Running",
                "started_at": "2026-10-21T01:00:00Z",
            }
        ],
        "log": "Opening job 201: SpeedCorp — Principal Cloud Architect\nNavigating to application form...\nFilled form field: personal.email\nCaptured screenshot of filled application.\n",
        "captcha": {"status": "idle"},
        "jobs": [
            make_job(
                job_id=201,
                company="SpeedCorp",
                title="Principal Cloud Architect",
                location="Remote",
                ats="greenhouse",
                status="Queued",
                can_submit=True,
                reason_code="ready",
                message="Ready to apply",
            )
        ],
        "queue": [
            {
                "id": 201,
                "job_id": 201,
                "company": "SpeedCorp",
                "title": "Principal Cloud Architect",
                "location": "Remote",
                "score": 95,
                "resume_version": "platform-sre",
                "status": "Queued",
                "job_status": "Queued",
                "can_submit": True,
                "readiness": {"ready": True, "reason_code": "ready", "message": "Ready to apply"},
                "ats": "greenhouse",
            }
        ],
        "preview": {
            "limit": 5,
            "count": 1,
            "items": [
                {
                    "job_id": 201,
                    "company": "SpeedCorp",
                    "title": "Principal Cloud Architect",
                    "location": "Remote",
                    "resume": "platform-sre",
                    "score": 95,
                    "ats": "greenhouse",
                }
            ],
        },
        "counts": [
            {"status": "Queued", "count": 1},
        ],
    },
    "mixed_outcome": {
        "active": "run-batch-888",
        "runs": [
            {
                "id": "run-batch-888",
                "action": "apply",
                "status": "Running",
                "started_at": "2026-10-21T01:05:00Z",
            }
        ],
        "log": "Opening job 301: AlphaTech — Systems Engineer\nSubmitting application...\n",
        "captcha": {"status": "idle"},
        "jobs": [
            # 1. Ready
            make_job(
                job_id=301,
                company="AlphaTech",
                title="Systems Engineer",
                location="Remote",
                ats="greenhouse",
                status="Queued",
                can_submit=True,
                reason_code="ready",
                message="Ready for submission",
            ),
            # 2. Queued (blocked by missing answer)
            make_job(
                job_id=302,
                company="BetaAI",
                title="NLP Researcher",
                location="Boston, MA",
                ats="lever",
                status="Queued",
                can_submit=False,
                reason_code="missing_answer",
                message="Missing response to screening questions",
                recovery_action="Answer custom employer questions",
                recovery_category="input",
            ),
            # 3. Submitted unverified (step='submit' reached, but status != 'Applied', awaiting receipt)
            make_job(
                job_id=303,
                company="GammaData",
                title="Data Architect",
                location="New York, NY",
                ats="greenhouse",
                status="Queued",
                can_submit=False,
                reason_code="submission_dispatched",
                message="Submit dispatched; receipt verification pending",
                submitted=True,
            ),
            # 4. Verified applied (receipt confirmed, status='Applied')
            make_job(
                job_id=304,
                company="DeltaCloud",
                title="Security Specialist",
                location="Remote",
                ats="greenhouse",
                status="Applied",
                can_submit=False,
                reason_code="already_applied",
                message="Confirmed with receipt evidence",
                submitted=True,
                application_mode="ONE_CLICK_AUTOMATED",
            ),
            # 5. Failed temporary (transient failure, retry backoff)
            make_job(
                job_id=305,
                company="EpsilonSec",
                title="DevOps Lead",
                location="Chicago, IL",
                ats="greenhouse",
                status="FailedTemporary",
                can_submit=False,
                reason_code="retry_not_due",
                message="Gateway timeout; retry backoff scheduled",
                next_attempt_at="2026-10-21T02:00:00Z",
                attempts=1,
                recovery_action="Wait for scheduled retry backoff",
                recovery_category="retry",
            ),
            # 6. Skipped
            make_job(
                job_id=306,
                company="ZetaCorp",
                title="Junior Intern",
                location="San Jose, CA",
                ats="workday",
                status="Skipped",
                can_submit=False,
                reason_code="low_match_score",
                message="Score below minimum threshold",
            ),
            # 7. Needs human action (ManualReview)
            make_job(
                job_id=307,
                company="EtaEnterprises",
                title="Risk Analyst",
                location="Remote",
                ats="lever",
                status="ManualReview",
                can_submit=False,
                reason_code="manual_review_needed",
                message="Visa sponsorship requires manual review",
                recovery_action="Review application before manual submission",
                recovery_category="manual",
            ),
        ],
        "queue": [
            {
                "id": 301,
                "job_id": 301,
                "company": "AlphaTech",
                "title": "Systems Engineer",
                "location": "Remote",
                "score": 88,
                "resume_version": "backend",
                "status": "Queued",
                "job_status": "Queued",
                "can_submit": True,
                "ats": "greenhouse",
                "readiness": {"ready": True, "reason_code": "ready", "message": "Ready for submission"},
            },
            {
                "id": 302,
                "job_id": 302,
                "company": "BetaAI",
                "title": "NLP Researcher",
                "location": "Boston, MA",
                "score": 84,
                "resume_version": "backend",
                "status": "Queued",
                "job_status": "Queued",
                "can_submit": False,
                "ats": "lever",
                "readiness": {"ready": False, "reason_code": "missing_answer", "message": "Missing response to screening questions"},
            },
            {
                "id": 303,
                "job_id": 303,
                "company": "GammaData",
                "title": "Data Architect",
                "location": "New York, NY",
                "score": 80,
                "resume_version": "backend",
                "status": "Queued",
                "job_status": "Queued",
                "can_submit": False,
                "ats": "greenhouse",
                "readiness": {"ready": False, "reason_code": "submission_dispatched", "message": "Submit dispatched; receipt verification pending"},
            },
        ],
        "preview": {
            "limit": 5,
            "count": 1,
            "items": [
                {
                    "job_id": 301,
                    "company": "AlphaTech",
                    "title": "Systems Engineer",
                    "location": "Remote",
                    "resume": "backend",
                    "score": 88,
                    "ats": "greenhouse",
                }
            ],
        },
        "counts": [
            {"status": "Queued", "count": 3},
            {"status": "Applied", "count": 1},
            {"status": "FailedTemporary", "count": 1},
            {"status": "Skipped", "count": 1},
            {"status": "ManualReview", "count": 1},
        ],
    },
    "large_queue": {
        "jobs": [
            make_job(
                job_id=1000 + i,
                company=f"Company {i+1:02d}",
                title=f"Engineer {i+1:02d}",
                location="Remote",
                ats="greenhouse" if i % 2 == 0 else "lever",
                status="Queued",
                score=90 - (i % 30),
                can_submit=(i % 3 == 0),
                reason_code="ready" if (i % 3 == 0) else "missing_answer",
                message="Ready to apply" if (i % 3 == 0) else "Missing answers",
            )
            for i in range(60)
        ],
        "queue": [
            {
                "id": 1000 + i,
                "job_id": 1000 + i,
                "company": f"Company {i+1:02d}",
                "title": f"Engineer {i+1:02d}",
                "location": "Remote",
                "score": 90 - (i % 30),
                "resume_version": "backend",
                "status": "Queued",
                "job_status": "Queued",
                "ats": "greenhouse" if i % 2 == 0 else "lever",
                "can_submit": (i % 3 == 0),
                "readiness": {
                    "ready": (i % 3 == 0),
                    "reason_code": "ready" if (i % 3 == 0) else "missing_answer",
                    "message": "Ready to apply" if (i % 3 == 0) else "Missing answers",
                },
            }
            for i in range(60)
        ],
        "counts": [
            {"status": "Queued", "count": 60},
        ],
    },
    "recovery": {
        "jobs": [
            make_job(
                job_id=401,
                company="AnswerNeeded Corp",
                title="Python Developer",
                location="Remote",
                ats="lever",
                status="Queued",
                can_submit=False,
                reason_code="missing_answer",
                message="Unanswered custom question: years of Python",
                recovery_action="Answer custom employer questions",
                recovery_category="input",
            ),
            make_job(
                job_id=402,
                company="ResumeMissing Inc",
                title="DevOps Engineer",
                location="Remote",
                ats="greenhouse",
                status="Queued",
                can_submit=False,
                reason_code="resume_missing",
                message="Missing tailored resume PDF",
                recovery_action="Assign a tailored resume in Application Queue",
                recovery_category="input",
            ),
            make_job(
                job_id=403,
                company="LoginRequired LLC",
                title="Fullstack Lead",
                location="Remote",
                ats="greenhouse",
                status="Queued",
                can_submit=False,
                reason_code="login_required",
                message="Portal login session required before submitting",
                apply_url="https://portal.greenhouse.io/login",
                recovery_action="Open portal login session",
                recovery_category="manual",
            ),
            make_job(
                job_id=404,
                company="RetryTransient Corp",
                title="Cloud Engineer",
                location="Seattle, WA",
                ats="greenhouse",
                status="FailedTemporary",
                can_submit=False,
                reason_code="retry_not_due",
                message="Transient network error; retry scheduled for 2026-10-21T02:00:00Z",
                next_attempt_at="2026-10-21T02:00:00Z",
                attempts=1,
                recovery_action="Wait for scheduled retry backoff",
                recovery_category="retry",
            ),
            make_job(
                job_id=405,
                company="UnsupportedATS Co",
                title="Site Engineer",
                location="Remote",
                ats="workday",
                status="Queued",
                can_submit=False,
                reason_code="unsupported_adapter",
                message="ATS 'workday' does not support one-click submission. Apply manually.",
                apply_url="https://workday.example/apply/405",
                recovery_action="Apply manually on the employer portal",
                recovery_category="manual",
            ),
        ],
        "queue": [
            {
                "id": 401,
                "job_id": 401,
                "company": "AnswerNeeded Corp",
                "title": "Python Developer",
                "location": "Remote",
                "score": 85,
                "resume_version": "backend",
                "status": "Queued",
                "job_status": "Queued",
                "can_submit": False,
                "readiness": {"ready": False, "reason_code": "missing_answer", "message": "Unanswered custom question"},
                "ats": "lever",
            },
            {
                "id": 402,
                "job_id": 402,
                "company": "ResumeMissing Inc",
                "title": "DevOps Engineer",
                "location": "Remote",
                "score": 82,
                "resume_version": "",
                "status": "Queued",
                "job_status": "Queued",
                "can_submit": False,
                "readiness": {"ready": False, "reason_code": "resume_missing", "message": "Missing tailored resume"},
                "ats": "greenhouse",
            },
        ],
        "captcha": {
            "status": "WaitingForUser",
            "company": "LoginRequired LLC",
            "job_id": 403,
            "phase": "application",
            "challenge_id": "ch_recovery_999",
        },
    },
}
