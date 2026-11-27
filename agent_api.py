"""Agent API routes under /api/agent.

This first version is a stub: it returns fixture plans from memory so the
dashboard can be built before the planner and executor exist. Every stub
response carries the header ``X-Agent-Stub: true``. The agent core branch
replaces the stub store with the real planner and executor without changing
the routes or response shapes in docs/agent-api.md.

Nothing here runs discovery or submits applications.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from agent.contracts import (
    TOOL_SPECS, TERMINAL_PLAN_STATUSES, AgentMode, AgentSettings, AnswerSuggestion,
    ChatReply, Plan, PlanStatus, Snippet, Step, StepStatus, ToolSpec,
)

router = APIRouter(prefix='/api/agent', tags=['agent'])

_lock = threading.Lock()
_plans: dict[str, Plan] = {}
_settings = AgentSettings()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _stub(response: Response) -> None:
    response.headers['X-Agent-Stub'] = 'true'


def reset_stub_state() -> None:
    """Clear stub plans and settings. Used by tests."""
    global _settings
    with _lock:
        _plans.clear()
        _settings = AgentSettings()


class PlanInput(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class SettingsInput(BaseModel):
    mode: AgentMode


_FIXTURE_STEPS = (
    ('status', {}, 'Check the tracker and queue before doing anything.'),
    ('discover', {'limit': 50}, 'Look for new roles that match the goal.'),
    ('score', {'limit': 100}, 'Score the new roles against your profile.'),
    ('queue', {'limit': 20}, 'Queue the qualified roles for applications.'),
    ('apply_preview', {'limit': 10}, 'Check which queued roles can be submitted safely.'),
    ('apply', {'job_ids': [101, 102]}, 'Submit the ready roles through validated adapters.'),
)


def _fixture_plan(goal: str) -> Plan:
    plan_id = uuid.uuid4().hex[:12]
    stamp = _now()
    steps = [Step(id=f'{plan_id}-{index}', index=index, tool=tool, args=dict(args), reason=reason)
             for index, (tool, args, reason) in enumerate(_FIXTURE_STEPS)]
    return Plan(id=plan_id, goal=goal, mode=_settings.mode, steps=steps, model='stub',
                summary='Stub plan: discover, score and queue on their own, then ask before submitting.',
                created_at=stamp, updated_at=stamp)


def _advance(plan: Plan) -> None:
    """Move a running stub plan forward by one step per read, for UI polling."""
    if plan.status != PlanStatus.RUNNING:
        return
    stamp = _now()
    for step in plan.steps:
        if step.status == StepStatus.RUNNING:
            step.status = StepStatus.DONE
            step.finished_at = stamp
            step.result_summary = f'Stub result for {step.tool}.'
            step.log.append(f'{stamp} finished {step.tool}')
            break
        if step.status == StepStatus.PENDING:
            if step.tool == 'apply':
                step.status = StepStatus.NEEDS_USER
                step.needs_user_reason = 'Submitting applications needs your approval.'
                step.job_id = step.args.get('job_ids', [None])[0]
                plan.status = PlanStatus.NEEDS_USER
            else:
                step.status = StepStatus.RUNNING
                step.started_at = stamp
                step.log.append(f'{stamp} started {step.tool}')
            break
    else:
        plan.status = PlanStatus.DONE
    plan.updated_at = stamp


def _get(plan_id: str) -> Plan:
    plan = _plans.get(plan_id)
    if plan is None:
        raise HTTPException(404, 'Plan not found')
    return plan


@router.get('/tools', response_model=list[ToolSpec])
def agent_tools(response: Response):
    _stub(response)
    return list(TOOL_SPECS)


@router.get('/settings', response_model=AgentSettings)
def agent_settings(response: Response):
    _stub(response)
    return _settings


@router.put('/settings', response_model=AgentSettings)
def update_agent_settings(payload: SettingsInput, response: Response):
    global _settings
    _stub(response)
    with _lock:
        _settings = _settings.model_copy(update={'mode': payload.mode})
        return _settings


@router.post('/plan', response_model=Plan)
def create_plan(payload: PlanInput, response: Response):
    _stub(response)
    with _lock:
        plan = _fixture_plan(payload.goal.strip() or payload.goal)
        _plans[plan.id] = plan
        return plan


@router.get('/plans', response_model=list[Plan])
def list_plans(response: Response, limit: int = Query(20, ge=1, le=100)):
    _stub(response)
    with _lock:
        return sorted(_plans.values(), key=lambda p: p.created_at, reverse=True)[:limit]


@router.get('/plans/{plan_id}', response_model=Plan)
def read_plan(plan_id: str, response: Response):
    _stub(response)
    with _lock:
        plan = _get(plan_id)
        _advance(plan)
        return plan


@router.post('/plans/{plan_id}/approve', response_model=Plan)
def approve_plan(plan_id: str, response: Response):
    """Start a draft or paused plan, or resume one waiting on the user."""
    _stub(response)
    with _lock:
        plan = _get(plan_id)
        if plan.status in TERMINAL_PLAN_STATUSES:
            raise HTTPException(409, f'Plan is {plan.status.value}')
        if _settings.mode == AgentMode.OFF:
            raise HTTPException(409, 'Agent mode is off')
        for step in plan.steps:
            if step.status == StepStatus.NEEDS_USER:
                # The stub never submits; it records the approval and finishes the step.
                step.status = StepStatus.DONE
                step.needs_user_reason = None
                step.result_summary = 'Approved. Stub did not submit anything.'
                step.finished_at = _now()
        plan.status = PlanStatus.RUNNING
        plan.updated_at = _now()
        return plan


@router.post('/plans/{plan_id}/pause', response_model=Plan)
def pause_plan(plan_id: str, response: Response):
    _stub(response)
    with _lock:
        plan = _get(plan_id)
        if plan.status in TERMINAL_PLAN_STATUSES:
            raise HTTPException(409, f'Plan is {plan.status.value}')
        plan.status = PlanStatus.PAUSED
        plan.updated_at = _now()
        return plan


@router.post('/plans/{plan_id}/cancel', response_model=Plan)
def cancel_plan(plan_id: str, response: Response):
    _stub(response)
    with _lock:
        plan = _get(plan_id)
        if plan.status not in TERMINAL_PLAN_STATUSES:
            plan.status = PlanStatus.CANCELLED
            for step in plan.steps:
                if step.status in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.NEEDS_USER):
                    step.status = StepStatus.SKIPPED
            plan.updated_at = _now()
        return plan


_FIXTURE_SNIPPET = Snippet(kind='fact', source_id='notice_period', text='Notice period: 30 days', score=0.92)


@router.post('/chat', response_model=ChatReply)
def agent_chat(payload: ChatInput, response: Response):
    _stub(response)
    text = payload.message.strip()
    lower = text.casefold()
    if lower.startswith(('plan ', 'goal ')) or 'apply to' in lower or 'find' in lower:
        with _lock:
            plan = _fixture_plan(text)
            _plans[plan.id] = plan
        return ChatReply(reply='Here is a plan. Approve it to start.', plan=plan)
    if 'notice' in lower:
        suggestion = AnswerSuggestion(question=text, answer='30 days', confidence=0.8, evidence=[_FIXTURE_SNIPPET])
        return ChatReply(reply='I found a saved fact that answers this. Confirm before I use it.',
                         suggestions=[suggestion])
    return ChatReply(reply='Stub agent: ask me to "find backend roles" or "plan apply to 5 remote jobs".')


@router.get('/memory/search', response_model=list[Snippet])
def memory_search(response: Response, q: str = Query(min_length=1, max_length=500),
                  k: int = Query(5, ge=1, le=20)):
    _stub(response)
    return [_FIXTURE_SNIPPET][:k]
