import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

import jobpilot
from agent.contracts import (
    TOOL_SPECS, TOOLS_BY_NAME, AgentMode, AnswerSuggestion, Plan, Retriever, Snippet, Step,
)


class ContractTests(unittest.TestCase):
    def test_submitting_tools_are_never_unattended_safe(self):
        for spec in TOOL_SPECS:
            if spec.submits_applications:
                self.assertFalse(spec.unattended_safe, spec.name)
        self.assertIn('apply', TOOLS_BY_NAME)

    def test_step_rejects_unknown_tool(self):
        with self.assertRaises(ValidationError):
            Step(id='x', index=0, tool='delete_everything', reason='no')

    def test_suggestion_needs_evidence_and_confirmation(self):
        snippet = Snippet(kind='fact', source_id='k', text='t', score=1.0)
        with self.assertRaises(ValidationError):
            AnswerSuggestion(question='q', answer='a', confidence=0.5, evidence=[])
        with self.assertRaises(ValidationError):
            AnswerSuggestion(question='q', answer='a', confidence=0.5, evidence=[snippet], requires_confirmation=False)

    def test_defaults_cost_nothing(self):
        from agent.contracts import AgentSettings, LLMProvider
        settings = AgentSettings()
        self.assertEqual(settings.provider, LLMProvider.OLLAMA)
        self.assertEqual(settings.embedder, 'local')
        self.assertFalse(settings.allow_paid_llm)
        with self.assertRaises(ValidationError):
            AgentSettings(provider='anthropic')
        self.assertEqual(AgentSettings(provider='anthropic', allow_paid_llm=True).provider, LLMProvider.ANTHROPIC)

    def test_snippet_rejects_unknown_kind(self):
        with self.assertRaises(ValidationError):
            Snippet(kind='secret', source_id='k', text='t', score=1.0)

    def test_retriever_protocol_is_structural(self):
        class Fake:
            def index_all(self, db): return 0
            def index_item(self, kind, source_id, text, metadata=None): pass
            def retrieve(self, query, k=5, kinds=None): return []
            def suggest_answer(self, question, job_context=None): return None
        self.assertIsInstance(Fake(), Retriever)

    def test_plan_round_trips_through_json(self):
        plan = Plan(id='p', goal='g', steps=[Step(id='s', index=0, tool='status', reason='r')],
                    created_at='t', updated_at='t')
        self.assertEqual(Plan.model_validate_json(plan.model_dump_json()), plan)


class AgentApiStubTests(unittest.TestCase):
    def setUp(self):
        import agent_server, agent_api
        from fastapi.testclient import TestClient
        (jobpilot.ROOT / 'tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=jobpilot.ROOT / 'tmp')
        root = Path(self.tmp.name)
        config = root / 'config'; config.mkdir()
        for name, value in [('profile.json', {'personal': {}}), ('answers.json', {}),
                            ('eligibility.json', {}), ('companies.json', [])]:
            (config / name).write_text(json.dumps(value), encoding='utf-8')
        self.patches = [patch('jobpilot.DATA', root), patch('jobpilot.DB', root / 'test.db'),
                        patch('jobpilot.CONFIG', config)]
        for p in self.patches: p.start()
        agent_api.reset_stub_state()
        agent_server.initialize()
        self.client = TestClient(agent_server.app, base_url='http://127.0.0.1:8765')
        self.headers = {'X-Jobpilot-Request': 'local-dashboard'}

    def tearDown(self):
        self.client.close()
        for p in reversed(self.patches): p.stop()
        jobpilot._JSON_CACHE.clear()
        self.tmp.cleanup()

    def test_tools_and_settings(self):
        response = self.client.get('/api/agent/tools')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['x-agent-stub'], 'true')
        self.assertEqual({t['name'] for t in response.json()}, set(TOOLS_BY_NAME))
        current = self.client.get('/api/agent/settings').json()
        self.assertEqual((current['mode'], current['provider'], current['allow_paid_llm']), ('safe_auto', 'ollama', False))
        updated = self.client.put('/api/agent/settings', json={'mode': 'plan_only'}, headers=self.headers)
        self.assertEqual(updated.json()['mode'], 'plan_only')
        self.assertEqual(self.client.put('/api/agent/settings', json={'mode': 'yolo'}, headers=self.headers).status_code, 422)

    def test_writes_need_dashboard_header(self):
        self.assertEqual(self.client.post('/api/agent/plan', json={'goal': 'x'}).status_code, 403)

    def test_plan_lifecycle_pauses_before_apply(self):
        plan = self.client.post('/api/agent/plan', json={'goal': 'apply to 5 remote roles'}, headers=self.headers).json()
        self.assertEqual(plan['status'], 'draft')
        self.assertEqual(plan['steps'][-1]['tool'], 'apply')
        self.client.post(f"/api/agent/plans/{plan['id']}/approve", headers=self.headers)
        for _ in range(20):
            current = self.client.get(f"/api/agent/plans/{plan['id']}").json()
            if current['status'] != 'running': break
        self.assertEqual(current['status'], 'needs_user')
        apply_step = current['steps'][-1]
        self.assertEqual(apply_step['status'], 'needs_user')
        self.assertTrue(apply_step['needs_user_reason'])
        self.assertTrue(all(s['status'] == 'done' for s in current['steps'][:-1]))
        self.client.post(f"/api/agent/plans/{plan['id']}/approve", headers=self.headers)
        self.assertEqual(self.client.get(f"/api/agent/plans/{plan['id']}").json()['status'], 'done')

    def test_pause_cancel_and_missing_plan(self):
        plan = self.client.post('/api/agent/plan', json={'goal': 'g'}, headers=self.headers).json()
        self.assertEqual(self.client.post(f"/api/agent/plans/{plan['id']}/pause", headers=self.headers).json()['status'], 'paused')
        cancelled = self.client.post(f"/api/agent/plans/{plan['id']}/cancel", headers=self.headers).json()
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertTrue(all(s['status'] == 'skipped' for s in cancelled['steps']))
        self.assertEqual(self.client.post(f"/api/agent/plans/{plan['id']}/approve", headers=self.headers).status_code, 409)
        self.assertEqual(self.client.get('/api/agent/plans/nope').status_code, 404)
        self.assertEqual(len(self.client.get('/api/agent/plans').json()), 1)

    def test_chat_and_memory_search(self):
        reply = self.client.post('/api/agent/chat', json={'message': 'what is my notice period'}, headers=self.headers).json()
        self.assertTrue(reply['suggestions'][0]['requires_confirmation'])
        planned = self.client.post('/api/agent/chat', json={'message': 'find backend roles'}, headers=self.headers).json()
        self.assertEqual(planned['plan']['status'], 'draft')
        hits = self.client.get('/api/agent/memory/search', params={'q': 'notice'}).json()
        self.assertEqual(hits[0]['kind'], 'fact')


if __name__ == '__main__':
    unittest.main()
