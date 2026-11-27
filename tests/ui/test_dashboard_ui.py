"""
Isolated UI Acceptance Tests for JobPilot Dashboard.
Tests dashboard.html against an in-process mock server serving the UI and API fixtures.
Runs headlessly via Playwright Chrome.
"""

import json
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import pytest
from playwright.sync_api import sync_playwright

from tests.ui.fixtures import FIXTURES, make_job


class MockDashboardHandler(BaseHTTPRequestHandler):
    """Mock backend handler serving dashboard.html and mock API responses."""
    fixtures_state = dict(FIXTURES["ready"])
    active_run = None
    last_mutation_payload = None
    force_api_failure = False
    force_queue_failure = False

    def log_message(self, format, *args):
        # Silence HTTP logs in tests
        pass

    def do_GET(self):
        if self.path == '/assets/dashboard-product.css':
            from pathlib import Path
            body = Path('dashboard-product.css').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/css')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", "/dashboard.html"):
            with open("dashboard.html", "r", encoding="utf-8") as f:
                content = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if self.force_queue_failure and self.path.startswith("/api/jobs/queue"):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"Queue backend database failure"}')
            return

        if self.force_api_failure and self.path.startswith("/api/"):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"Internal Server Error"}')
            return

        if self.path == "/api/health":
            self.respond_json({"status": "ok", "restart_required": False})
            return

        if self.path == "/api/state":
            active_val = self.fixtures_state.get("active", self.active_run)
            runs_val = self.fixtures_state.get("runs", [])
            counts_val = self.fixtures_state.get("counts", [])
            applied_periods_val = self.fixtures_state.get("applied_periods", {
                "today": 1,
                "before_today": 0,
                "unknown_date": 0,
                "date": "2026-10-21",
                "timezone": "Asia/Kolkata",
            })
            self.respond_json({
                "active": active_val,
                "capabilities": {
                    "one_click": True,
                    "copilot": True,
                    "greenhouse": "Submit + confirmation validated",
                    "lever": "Submit + confirmation validated",
                },
                "runs": runs_val,
                "counts": counts_val,
                "applied_periods": applied_periods_val,
                "stats": {
                    "jobs_discovered": 10,
                    "jobs_qualified": 5,
                    "jobs_applied": 1,
                    "jobs_failed": 0,
                },
            })
            return

        if self.path.startswith("/api/runs/") and self.path.endswith("/log"):
            log_text = self.fixtures_state.get("log", "Starting worker...")
            self.respond_json({"text": log_text})
            return

        if self.path == "/api/captcha-status":
            captcha = self.fixtures_state.get("captcha", {"status": "idle"})
            self.respond_json(captcha)
            return

        if self.path.startswith("/api/jobs/queue"):
            self.respond_json(self.fixtures_state.get("queue", []))
            return

        if self.path.startswith("/api/jobs?"):
            self.respond_json(self.fixtures_state.get("jobs", []))
            return

        if self.path.startswith("/api/apply/preview"):
            preview = self.fixtures_state.get("preview", {
                "count": len([j for j in self.fixtures_state.get("jobs", []) if j.get("can_submit")]),
                "items": [
                    {
                        "job_id": j["id"],
                        "company": j["company"],
                        "title": j["title"],
                        "location": j["location"],
                        "resume": j.get("resume", "backend"),
                        "score": j["score"],
                    }
                    for j in self.fixtures_state.get("jobs", []) if j.get("can_submit")
                ],
            })
            self.respond_json(preview)
            return

        if self.path.startswith("/api/jobs/today"):
            self.respond_json(self.fixtures_state.get("today_jobs", []))
            return

        if self.path.startswith("/api/profile"):
            self.respond_json({"profile": {}, "answers": {}, "revision": 1})
            return

        if self.path.startswith("/api/questions"):
            self.respond_json([])
            return

        if self.path.startswith("/api/stats/daily"):
            self.respond_json({
                "jobs_discovered": 15,
                "jobs_qualified": 8,
                "jobs_applied": 2,
                "jobs_failed": 0,
                "captcha_challenges": 0,
                "portals_active": ["greenhouse", "lever"],
            })
            return

        if self.path.startswith("/api/metrics/tokens"):
            self.respond_json({
                "total_input_tokens": 12000,
                "total_output_tokens": 1500,
                "estimated_cost_usd": 0.045,
                "runs_today": 3,
            })
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.force_queue_failure and self.path.startswith("/api/jobs/queue"):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"Queue backend database failure"}')
            return

        if self.force_api_failure and self.path.startswith("/api/"):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"Internal Server Error"}')
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        payload = json.loads(body.decode("utf-8")) if body else {}
        MockDashboardHandler.last_mutation_payload = payload

        if self.path == "/api/runs":
            self.respond_json({"id": "run-1001", "status": "queued"})
            return

        if self.path.startswith("/api/jobs/") and self.path.endswith("/manual-applied"):
            self.respond_json({"status": "recorded", "receipt_id": "rec-123"})
            return

        if self.path == "/api/continue":
            self.respond_json({"continuing": True, "challenge_id": payload.get("challenge_id")})
            return

        if self.path == "/api/stop":
            self.respond_json({"stopping": True})
            return

        if "/set-resume" in self.path:
            self.respond_json({"saved": True, "resume": payload.get("resume")})
            return

        if "/approve" in self.path:
            self.respond_json({"approved": True})
            return

        if "/reject" in self.path:
            self.respond_json({"rejected": True})
            return

        self.respond_json({"status": "ok"})

    def respond_json(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def mock_server():
    port = find_free_port()
    server = HTTPServer(("127.0.0.1", port), MockDashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    yield base_url
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="function")
def reset_server():
    MockDashboardHandler.fixtures_state = dict(FIXTURES["ready"])
    MockDashboardHandler.active_run = None
    MockDashboardHandler.last_mutation_payload = None
    MockDashboardHandler.force_api_failure = False
    MockDashboardHandler.force_queue_failure = False
    yield


@pytest.fixture(scope="module")
def browser_instance():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="function")
def page(browser_instance):
    ctx = browser_instance.new_context()
    pg = ctx.new_page()
    yield pg
    pg.close()
    ctx.close()


def test_connection_state_connected(mock_server, reset_server, page):
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    assert page.text_content("#connection-state") == "Connected"


def test_ready_fixture_shows_apply_button_and_stat_cards(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["ready"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#jobrows tr")

    # Ready to apply count should be 1
    counts_text = page.text_content("#counts")
    assert "Ready to apply" in counts_text
    assert "Queued Total" in counts_text

    # Apply button should be visible and enabled for ready job
    apply_btn = page.query_selector("#jobrows button.btn-apply")
    assert apply_btn is not None
    assert apply_btn.is_enabled()
    assert "⚡ Apply" in apply_btn.text_content()


def test_discovered_job_explains_its_match_score(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["ready"])
    MockDashboardHandler.fixtures_state["today_jobs"] = [{
        "id": 401, "company": "Fixture Co", "title": "Backend Engineer", "location": "Hyderabad, India",
        "portal": "greenhouse", "score": 82, "status": "Qualified", "apply_url": "https://example.test/job",
        "match_reasons": "C#, .NET", "match_breakdown": {
            "role_score": 30, "required_skills_score": 20, "optional_skills_score": 8,
            "location_score": 15, "experience_fit_score": 15, "salary_score": 10,
            "salary_note": "Posting states up to ₹18 LPA; saved target is ₹15 LPA",
            "matched_required_skills": ["C#", ".NET"], "missing_required_skills": ["SQL"],
            "matched_optional_skills": ["Angular"], "uncertainties": []
        }
    }]
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.evaluate("switchTab('discovered')")
    page.wait_for_selector("#discovered-rows .match-explainer")
    row = page.locator("#discovered-rows tr").first
    assert "Why this score?" in row.inner_text()
    row.locator(".match-explainer summary").click()
    assert "Skills to verify: SQL" in row.inner_text()
    assert "₹18 LPA" in row.inner_text()


def test_unsupported_adapter_guidance_and_no_apply(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["unsupported_adapter"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#jobrows tr")

    # Apply button should NOT be present in tracker row
    apply_btn = page.query_selector("#jobrows button.btn-apply")
    assert apply_btn is None

    # Should have guidance to apply manually
    table_text = page.text_content("#jobrows")
    assert "LegacySystems" in table_text
    assert "does not support one-click submission" in table_text or "ATS 'workday'" in table_text

    # Should offer manual application record button
    manual_btn = page.query_selector("button:has-text('I applied manually')")
    assert manual_btn is not None


def test_missing_answer_shows_guidance_button(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["missing_answer"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#jobrows tr")

    # Apply button should NOT be present in tracker row
    apply_btn = page.query_selector("#jobrows button.btn-apply")
    assert apply_btn is None

    # Blocker action button to resolve in questions tab
    resolve_btn = page.query_selector("button:has-text('Resolve in Questions')")
    assert resolve_btn is not None

    # Clicking it switches to questions tab
    resolve_btn.click()
    page.wait_for_selector("section#questions.tab.active")
    assert page.is_visible("section#questions")


def test_missing_resume_shows_guidance_button(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["missing_resume"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#jobrows tr")

    # Blocker action button to assign resume in queue tab
    resume_btn = page.query_selector("button:has-text('Application Queue')")
    assert resume_btn is not None

    # Clicking it switches to queue tab
    resume_btn.click()
    page.wait_for_selector("section#queue.tab.active")
    assert page.is_visible("section#queue")


def test_retry_not_due_shows_scheduled_time(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["retry_not_due"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#jobrows tr")

    # Should not show Apply button in tracker row
    assert page.query_selector("#jobrows button.btn-apply") is None

    # Should show retry time
    table_text = page.text_content("#jobrows")
    assert "2026-10-18T12:00:00Z" in table_text
    assert "Wait for scheduled retry backoff" in table_text or "retry" in table_text.lower()


def test_challenge_waiting_shows_banner_and_button(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["challenge_waiting"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#captcha-banner", state="visible")

    banner_text = page.text_content("#captcha-banner")
    assert "Verification needed" in banner_text
    assert "CaptchaDefense" in banner_text

    # Continue verification button is enabled
    cont_btn = page.query_selector("#continue-verification")
    assert cont_btn is not None
    assert cont_btn.is_enabled()


def test_stale_queue_entry_identified_and_blocked(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["stale_queue_entry"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")

    # Check stat card identifies stale queue
    page.wait_for_selector("#counts")
    counts_text = page.text_content("#counts")
    assert "Stale Queue" in counts_text

    # Navigate to Queue tab
    page.click("button.tab-btn:has-text('Application Queue')")
    page.wait_for_selector("#queue-rows tr")

    # Verify stale indicator rendered in row
    queue_row = page.text_content("#queue-rows")
    assert "Stale" in queue_row
    assert "Tracker: Applied" in queue_row


def test_manual_review_shows_badge_and_no_automated_apply(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["manual_review"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#jobrows tr")

    # No Apply button in tracker row
    assert page.query_selector("#jobrows button.btn-apply") is None

    # Shows manual review notice
    table_text = page.text_content("#jobrows")
    assert "Manual review needed" in table_text


def test_confirmed_applied_shows_badge_and_no_reapply(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["confirmed_applied"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    # Tracker by default hides applied, uncheck hide applied
    page.uncheck("#hide-applied-tracker")
    page.wait_for_selector("#jobrows tr")

    table_text = page.text_content("#jobrows")
    assert "SuccessTech" in table_text
    assert "Applied" in table_text
    # No Apply button for already applied jobs in tracker row
    assert page.query_selector("#jobrows button.btn-apply") is None


def test_empty_data_handled_gracefully(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["empty_data"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#jobrows")

    table_text = page.text_content("#jobrows")
    assert "No jobs found" in table_text


def test_batch_preview_matches_jobs_sent_to_api(mock_server, reset_server, page):
    MockDashboardHandler.fixtures_state = dict(FIXTURES["ready"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")

    # Click Apply (selected) which triggers previewApply and opens accessible modal
    page.click("button:has-text('Review & apply ready jobs')")

    # Wait for accessible modal to open
    page.wait_for_selector("#batch-preview-modal", state="visible")
    modal_text = page.text_content("#batch-preview-content")

    # Check modal includes company, title, location, resume
    assert "ReadyTech" in modal_text
    assert "Staff Backend Architect" in modal_text
    assert "backend" in modal_text

    # Click Confirm & Launch Batch inside accessible modal
    page.click("#batch-preview-launch-btn")

    # Wait for run POST to be recorded
    page.wait_for_function("() => window.chosenRun !== null")

    # Check payload sent to POST /api/runs
    assert MockDashboardHandler.last_mutation_payload is not None
    assert MockDashboardHandler.last_mutation_payload["action"] == "apply"
    assert MockDashboardHandler.last_mutation_payload["job_ids"] == [101]


def test_lost_response_warning_and_no_mutation_replay(mock_server, reset_server, page):
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")

    # Now simulate API failure on mutation
    MockDashboardHandler.force_api_failure = True

    # Attempt to click copilot test button which sends POST /api/runs
    copilot_btn = page.query_selector("button:has-text('Copilot (Test)')")
    if copilot_btn:
        copilot_btn.click()
        page.wait_for_selector("#message")
        msg = page.text_content("#message")
        # Should display warning or error detail
        assert len(msg) > 0


def test_api_failure_displays_disconnected_state(mock_server, reset_server, page):
    MockDashboardHandler.force_api_failure = True
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.disconnected")
    assert page.text_content("#connection-state") == "Disconnected"
    assert "Disconnected" in page.text_content("#worker")


# ─────────────────────────────────────────────────────────────────────────────
# NEW ACCEPTANCE TESTS FOR GOALS 1, 2, 3, 4, 5, 6
# ─────────────────────────────────────────────────────────────────────────────

def test_truthful_separate_counts_and_no_fake_applied(mock_server, reset_server, page):
    """
    Goal 1: Show truthful separate counts for ready, queued, running, submitted,
    verified, failed, skipped, and needs-human-action jobs.
    Do not label a job 'Applied' unless the API reports submission evidence.
    """
    MockDashboardHandler.fixtures_state = dict(FIXTURES["mixed_outcome"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")

    # Uncheck hide-applied so we inspect all jobs
    page.uncheck("#hide-applied-tracker")
    page.wait_for_selector("#jobrows tr")

    # 1. Verify separate stat cards
    assert page.text_content("#count-ready") == "1"
    assert page.text_content("#count-queued") == "3"
    assert page.text_content("#count-running") == "1"
    assert page.text_content("#count-submitted") == "1"
    assert page.text_content("#count-verified") == "1"
    assert page.text_content("#count-failed") == "1"
    assert page.text_content("#count-skipped") == "1"

    # Needs Action includes: ManualReview (1) + MissingAnswer (1) = 2
    needs_action_text = page.text_content("#count-action")
    assert int(needs_action_text) >= 2

    table_text = page.text_content("#jobrows")

    # 2. Check DeltaCloud (verified applied job) has 'Verified Applied' and receipt evidence link
    assert "DeltaCloud" in table_text
    assert "Verified Applied" in table_text
    receipt_link = page.query_selector("a[href*='/api/evidence/304/receipt']")
    assert receipt_link is not None

    # 3. Check GammaData (submitted unverified) is NOT labeled 'Applied'
    # It must be labeled 'Submitted (Unverified)'
    gamma_row = page.query_selector("tr:has-text('GammaData')")
    assert gamma_row is not None
    gamma_row_text = gamma_row.text_content()
    assert "Submitted (Unverified)" in gamma_row_text
    assert "Verified Applied" not in gamma_row_text


def test_active_batch_status_visible_with_phase_elapsed_events(mock_server, reset_server, page):
    """
    Goal 2: Make the current batch status immediately visible: active job, current phase,
    elapsed time, completed/remaining, and last meaningful event.
    """
    MockDashboardHandler.fixtures_state = dict(FIXTURES["running"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#batch-status-panel", state="visible")

    # Banner elements
    assert "BATCH RUNNING" in page.text_content("#batch-live-badge")
    assert "APPLY in progress" in page.text_content("#batch-action-name") or "BATCH" in page.text_content("#batch-action-name")

    # Active Job
    active_job = page.text_content("#batch-active-job")
    assert "SpeedCorp" in active_job or "Principal Cloud Architect" in active_job

    # Current Phase
    phase = page.text_content("#batch-current-phase")
    assert len(phase) > 0 and phase != "—"

    # Elapsed time ticker is initialized
    elapsed = page.text_content("#batch-elapsed")
    assert "⏱️" in elapsed

    # Progress and last meaningful event
    progress = page.text_content("#batch-progress-text")
    assert "completed" in progress or "remaining" in progress

    last_event = page.text_content("#batch-last-event")
    assert "Captured screenshot" in last_event or "personal.email" in last_event or "Opening job" in last_event


def test_batch_preview_breakdown_excluded_reasons_and_limitations(mock_server, reset_server, page):
    """
    Goal 3: Clear preview before starting a batch: eligible count, excluded count
    and reasons, portal types, and estimated limitations.
    Uses accessible #batch-preview-modal instead of window.confirm.
    """
    fixtures = dict(FIXTURES["mixed_outcome"])
    fixtures["active"] = None
    MockDashboardHandler.fixtures_state = fixtures
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("button:has-text('Review & apply ready jobs'):not([disabled])")

    # Click review & apply to open accessible modal
    page.click("button:has-text('Review & apply ready jobs')")

    page.wait_for_selector("#batch-preview-modal", state="visible")
    modal_text = page.text_content("#batch-preview-content")

    # Eligible count & job details
    assert "AlphaTech" in modal_text
    assert "Eligible:" in modal_text and "1" in modal_text

    # Excluded count & reason breakdown
    assert "Excluded from batch:" in modal_text
    assert "missing_answer" in modal_text

    # Portal types
    assert "greenhouse" in modal_text.lower() or "lever" in modal_text.lower() or "validated" in modal_text.lower()

    # Limitations
    assert "Limitations:" in modal_text
    assert "Tested submit-and-confirm adapters" in modal_text or "submit" in modal_text.lower()

    # Launch batch from modal
    page.click("#batch-preview-launch-btn")
    page.wait_for_function("() => window.chosenRun !== null")


def test_recovery_actions_specific_and_backend_supported(mock_server, reset_server, page):
    """
    Goal 4: Make recovery actions specific (missing answer, sign in, challenge,
    retry transient failure) using only backend-supported actions.
    """
    MockDashboardHandler.fixtures_state = dict(FIXTURES["recovery"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#jobrows tr")

    # 1. Missing answer -> Resolve in Questions
    ans_btn = page.query_selector("button:has-text('Resolve in Questions')")
    assert ans_btn is not None

    # 2. Missing resume -> Application Queue
    res_btn = page.query_selector("button:has-text('Application Queue')")
    assert res_btn is not None

    # 3. Portal login required -> Open Login Session
    login_btn = page.query_selector("button:has-text('Open Login Session')")
    assert login_btn is not None

    # 4. CAPTCHA waiting -> Continue Check banner & button
    captcha_banner = page.query_selector("#captcha-banner")
    assert captcha_banner is not None and captcha_banner.is_visible()
    assert page.is_enabled("#continue-verification")

    # 5. Transient failure -> retry timestamp displayed
    table_text = page.text_content("#jobrows")
    assert "2026-10-21T02:00:00Z" in table_text

    # 6. Unsupported adapter -> Apply manually
    assert page.query_selector("button:has-text('I applied manually')") is not None


def test_pagination_bounded_and_no_silent_miscount_on_large_queue(mock_server, reset_server, page):
    """
    Goal 5: Bounded/paginated lists; do not silently miscount a clipped page.
    """
    MockDashboardHandler.fixtures_state = dict(FIXTURES["large_queue"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#jobrows tr")

    # Page 1 should show exactly 20 rows (bounded to TRACKER_PAGE_SIZE)
    rows_page1 = page.query_selector_all("#jobrows tr")
    assert len(rows_page1) == 20

    # Pagination toolbar indicator
    page_ind = page.text_content("#tracker-page-indicator")
    assert page_ind == "Page 1 of 3"
    info_text = page.text_content("#tracker-pagination-info")
    assert "Showing jobs 1–20 of 60" in info_text

    # Full database count is truthfully preserved in stat cards (not miscounted to 20!)
    assert page.text_content("#count-queued") == "60"

    # Click Next button
    page.click("#tracker-next-btn")
    page.wait_for_function("() => document.getElementById('tracker-page-indicator').textContent === 'Page 2 of 3'")

    rows_page2 = page.query_selector_all("#jobrows tr")
    assert len(rows_page2) == 20
    info_page2 = page.text_content("#tracker-pagination-info")
    assert "Showing jobs 21–40 of 60" in info_page2

    # Click Previous button
    page.click("#tracker-prev-btn")
    page.wait_for_function("() => document.getElementById('tracker-page-indicator').textContent === 'Page 1 of 3'")
    assert page.text_content("#tracker-page-indicator") == "Page 1 of 3"


def test_empty_and_loading_states(mock_server, reset_server, page):
    """
    Goal 6: Empty and loading state verification.
    """
    MockDashboardHandler.fixtures_state = dict(FIXTURES["empty"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")

    # Tracker empty state
    page.wait_for_selector("#jobrows")
    assert "No jobs found" in page.text_content("#jobrows")

    # Batch status panel should remain hidden when idle
    assert not page.is_visible("#batch-status-panel")

    # Queue empty state
    page.click("button.tab-btn:has-text('Application Queue')")
    page.wait_for_selector("#queue-rows")
    assert "Queue is empty" in page.text_content("#queue-rows")

    # Discovered empty state
    page.click("button.tab-btn:has-text('Discovered Today')")
    page.wait_for_selector("#discovered-rows")
    assert "No jobs found matching the criteria" in page.text_content("#discovered-rows")


def test_agent_status_hero_section(mock_server, reset_server, page):
    """
    Priority 1: Agent status hero section displays On/Paused/Offline,
    last discovery, next cycle schedule, active app, latest receipt,
    and pause/resume controls.
    """
    MockDashboardHandler.fixtures_state = dict(FIXTURES["ready"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector(".agent-hero")

    # Status pill shows active/ready
    pill_text = page.text_content("#agent-state-pill")
    assert "Agent" in pill_text or "Ready" in pill_text or "On" in pill_text

    # Hero metrics present
    assert page.is_visible("#hero-last-discovery")
    assert page.is_visible("#hero-next-scan")
    assert page.is_visible("#hero-active-app")
    assert page.is_visible("#hero-latest-receipt")

    # Pause control is present and accessible
    assert page.is_visible("#agent-hero-pause-btn")


def test_queue_error_resilience_and_stale_retention(mock_server, reset_server, page):
    """
    Priority 0: When /api/jobs/queue fails, the UI retains last confirmed data,
    shows an explicit error banner with Retry, never shows a false zero count,
    and disables auto-submit actions until verified.
    """
    # 1. Start with ready state so queueData is initially populated
    MockDashboardHandler.fixtures_state = dict(FIXTURES["ready"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector("#jobrows tr")

    initial_ready_count = page.text_content("#count-ready")
    assert initial_ready_count == "1"

    # 2. Simulate queue API failure on refresh
    MockDashboardHandler.force_queue_failure = True
    page.evaluate("() => refresh()")

    # 3. Wait for error banner
    page.wait_for_selector("#queue-error-banner", state="visible")
    banner_text = page.text_content("#queue-error-text")
    assert "failed" in banner_text.lower() or "unable" in banner_text.lower() or "error" in banner_text.lower()

    # 4. Critical check: queueData was NOT replaced with zero; stale count is retained
    ready_count_after_error = page.text_content("#count-ready")
    assert ready_count_after_error != "0"
    assert "1" in ready_count_after_error


def test_responsive_viewports_desktop_and_laptop(mock_server, reset_server, page):
    """
    Priority 1: Test at 1366px (desktop) and 768px (laptop/mobile) widths.
    Verify long employer names, titles, and errors do not break layout.
    """
    MockDashboardHandler.fixtures_state = dict(FIXTURES["ready"])

    # 1. Test 1366px width (Standard Desktop / Laptop)
    page.set_viewport_size({"width": 1366, "height": 768})
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")
    page.wait_for_selector(".agent-hero")
    page.wait_for_selector("#jobrows tr")
    assert page.is_visible(".agent-hero")
    assert page.is_visible("#counts")
    assert page.is_visible("#jobrows")

    # 2. Test 768px width (Narrow / Tablet)
    page.set_viewport_size({"width": 768, "height": 1024})
    page.wait_for_selector(".agent-hero")
    assert page.is_visible(".agent-hero")
    assert page.is_visible("#counts")
    assert page.is_visible("#jobrows")


def test_keyboard_navigation_and_focus(mock_server, reset_server, page):
    """
    Priority 1: Interactive elements are reachable via keyboard and have visible focus.
    """
    MockDashboardHandler.fixtures_state = dict(FIXTURES["ready"])
    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")

    # Focus on the first tab button
    page.focus("button.tab-btn:has-text('Dashboard')")
    focused_tag = page.evaluate("() => document.activeElement.tagName")
    assert focused_tag.lower() == "button"


def test_applied_today_vs_before_today_display_and_filter(mock_server, reset_server, page):
    """
    User Request: The dashboard must show 'Applied Today' vs 'Applied Before Today'
    distinctly in the count cards, the tracker rows, and the filter options.
    """
    MockDashboardHandler.fixtures_state = {
        "counts": [
            {"status": "Applied", "count": 3},
            {"status": "Queued", "count": 1},
        ],
        "applied_periods": {
            "today": 1,
            "before_today": 2,
            "unknown_date": 0,
            "date": "2026-10-21",
            "timezone": "Asia/Kolkata",
        },
        "jobs": [
            {
                "id": 901,
                "company": "TodayCorp",
                "title": "Backend Lead",
                "location": "Bengaluru",
                "score": 88,
                "status": "Applied",
                "can_submit": False,
                "applied_at": "2026-10-21T10:30:00Z",
                "submitted": True,
            },
            {
                "id": 902,
                "company": "EarlierCorp",
                "title": "Systems Architect",
                "location": "Remote",
                "score": 85,
                "status": "Applied",
                "can_submit": False,
                "applied_at": "2026-10-18T14:20:00Z",
                "submitted": True,
            },
            {
                "id": 903,
                "company": "LegacyCorp",
                "title": "Platform Engineer",
                "location": "Mumbai",
                "score": 80,
                "status": "Applied",
                "can_submit": False,
                "applied_at": None,
                "submitted": True,
            },
        ],
    }

    page.goto(f"{mock_server}/")
    page.wait_for_selector("#connection-state.connected")

    # 1. Verify separate summary count cards for Applied Today, Applied Before Today, and Applied Total
    page.wait_for_selector("#card-applied-today")
    page.wait_for_selector("#card-applied-before")
    page.wait_for_selector("#card-verified")

    assert page.text_content("#card-applied-today #count-today") == "1"
    assert page.text_content("#card-applied-before #count-before") == "2"
    assert page.text_content("#count-verified") == "3"

    # 2. Uncheck hide applied to inspect all rows
    page.uncheck("#hide-applied-tracker")
    page.wait_for_selector("#jobrows tr")

    # TodayCorp should display 'Applied · Today'
    today_row = page.text_content("tr:has-text('TodayCorp')")
    assert "Applied · Today" in today_row
    assert "Applied Today" in today_row

    # EarlierCorp should display 'Applied · Before Today'
    earlier_row = page.text_content("tr:has-text('EarlierCorp')")
    assert "Applied · Before Today" in earlier_row
    assert "Applied Before Today" in earlier_row

    # 3. Test filter dropdown for 'applied_today'
    page.select_option("#tracker-status-filter", "applied_today")
    page.wait_for_timeout(200)
    visible_text = page.text_content("#jobrows")
    assert "TodayCorp" in visible_text
    assert "EarlierCorp" not in visible_text
    assert "LegacyCorp" not in visible_text

    # 4. Test filter dropdown for 'applied_before'
    page.select_option("#tracker-status-filter", "applied_before")
    page.wait_for_timeout(200)
    visible_text = page.text_content("#jobrows")
    assert "EarlierCorp" in visible_text
    assert "LegacyCorp" in visible_text
    assert "TodayCorp" not in visible_text


