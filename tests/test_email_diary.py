"""Comprehensive unit & integration tests for JobPilot Email Reader & Job Diary."""
import os
import unittest
from unittest.mock import MagicMock, patch

import email_diary_service
import jobpilot


class TestEmailDiaryService(unittest.TestCase):
    def setUp(self):
        email_diary_service.init_diary_db()

    def test_add_get_update_delete_diary_entry(self):
        entry_id = email_diary_service.add_diary_entry(
            entry_type="hr_call",
            company="TestCorp",
            role="Senior Systems Engineer",
            contact_name="Alice Smith",
            contact_email="alice@testcorp.com",
            summary="Recruiter called to discuss distributed backend role and notice period.",
            action_items="Send follow-up with portfolio link.",
            status="pending",
            due_date="2026-10-30"
        )
        self.assertIsInstance(entry_id, int)
        self.assertGreater(entry_id, 0)

        # Retrieve
        entries = email_diary_service.get_diary_entries(entry_type="hr_call", status="pending")
        match = [e for e in entries if e["id"] == entry_id]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["company"], "TestCorp")
        self.assertEqual(match[0]["contact_name"], "Alice Smith")
        self.assertTrue(bool(match[0]["draft_subject"]))
        self.assertTrue(bool(match[0]["draft_body"]))

        # Update
        updated = email_diary_service.update_diary_entry(entry_id, status="completed", action_items="Sent follow-up email.")
        self.assertTrue(updated)

        entries_completed = email_diary_service.get_diary_entries(status="completed")
        self.assertTrue(any(e["id"] == entry_id for e in entries_completed))

        # Delete
        deleted = email_diary_service.delete_diary_entry(entry_id)
        self.assertTrue(deleted)

    def test_generate_followup_draft(self):
        draft = email_diary_service.generate_followup_draft(
            company="Stripe",
            contact_name="John Recruiter",
            role="Backend Engineer",
            notes="Discussed transaction processing and latency requirements.",
            context="hr_call"
        )
        self.assertIn("Stripe", draft["subject"])
        self.assertIn("Backend Engineer", draft["subject"])
        self.assertIn("Hi John Recruiter,", draft["body"])

    def test_classify_job_email(self):
        cat1, act1, _ = email_diary_service.classify_job_email(
            "Invitation to technical interview with engineering team",
            "recruiter@company.com",
            "We would like to invite you to a 45-minute coding interview on Zoom."
        )
        self.assertEqual(cat1, "interview_invite")
        self.assertIn("Schedule", act1)

        cat2, act2, _ = email_diary_service.classify_job_email(
            "An update on your application to Canonical",
            "no-reply@greenhouse-mail.io",
            "We regret to inform you that we shall not be progressing your application."
        )
        self.assertEqual(cat2, "rejection")

        cat3, act3, _ = email_diary_service.classify_job_email(
            "Found your profile on LinkedIn - Senior Developer Opportunity",
            "talent@techcorp.io",
            "I came across your profile and would love to discuss an open role."
        )
        self.assertEqual(cat3, "hr_followup")

    def test_handle_chat_hr_call_and_followup(self):
        msg = "agent this hr called me from Swiggy and followup with them"
        res = email_diary_service.handle_chat_diary_command(msg)
        self.assertIsNotNone(res)
        self.assertEqual(res["action"], "diary_entry_created")
        self.assertIn("Swiggy", res["reply"])
        self.assertIn("Tailored Follow-up Draft Ready", res["reply"])
        self.assertIn("draft", res)

    def test_handle_chat_pending_followups(self):
        # Ensure at least one pending entry exists
        email_diary_service.add_diary_entry(
            entry_type="hr_call",
            company="PendingTestCo",
            summary="Need to follow up regarding technical round schedule.",
            status="pending"
        )
        msg = "what are my pending job related followups"
        res = email_diary_service.handle_chat_diary_command(msg)
        self.assertIsNotNone(res)
        self.assertIn("pending follow-up", res["reply"])
        self.assertIn("PendingTestCo", res["reply"])

    def test_handle_chat_failures_rejections(self):
        msg = "show my failures and rejections"
        res = email_diary_service.handle_chat_diary_command(msg)
        self.assertIsNotNone(res)
        self.assertIn("Failures Tracker", res["reply"])

    def test_handle_chat_interviews(self):
        msg = "what interviews have i given"
        res = email_diary_service.handle_chat_diary_command(msg)
        self.assertIsNotNone(res)
        self.assertTrue("Interview Tracker" in res["reply"] or "no recorded interviews" in res["reply"])


if __name__ == "__main__":
    unittest.main()
