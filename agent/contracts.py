"""Shared contracts for the JobPilot agent.

This module is the single source of truth for the data that flows between
the planner, the executor, the memory (RAG) module and the dashboard. It has
no side effects and no dependencies beyond pydantic, so every branch can
import it safely.

Ownership:
- agent core (planner, executor, tools, llm): produces Plan and Step objects.
- agent/memory: implements the Retriever protocol and returns Snippet and
  AnswerSuggestion objects.
- dashboard: renders the JSON forms of these models from /api/agent/*.

Change this file only through the integrator, because all three parts depend
on it.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

CONTRACT_VERSION = '1'


class AgentMode(str, Enum):
    """How much the agent may do without a person."""

    OFF = 'off'                 # Agent disabled; the legacy runner loop is used.
    PLAN_ONLY = 'plan_only'     # The agent proposes plans; a person runs every step.
    SAFE_AUTO = 'safe_auto'     # Steps marked unattended_safe run on their own. Default.
    FULL = 'full'               # Everything may run, but submission still obeys
                                # autoSubmit, unattended and allowedAutoSubmitAts.


DEFAULT_AGENT_MODE = AgentMode.SAFE_AUTO


class StepStatus(str, Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    DONE = 'done'
    FAILED = 'failed'
    NEEDS_USER = 'needs_user'   # Paused for a person (approval, CAPTCHA, missing fact).
    SKIPPED = 'skipped'


class PlanStatus(str, Enum):
    DRAFT = 'draft'             # Proposed, waiting for approval.
    RUNNING = 'running'
    PAUSED = 'paused'           # Paused by the user.
    NEEDS_USER = 'needs_user'   # A step is waiting for a person.
    DONE = 'done'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


TERMINAL_PLAN_STATUSES = frozenset({PlanStatus.DONE, PlanStatus.FAILED, PlanStatus.CANCELLED})


class ToolSpec(BaseModel):
    """A tool the planner may call. Tools wrap existing JobPilot commands."""

    name: str
    description: str
    args_schema: dict[str, Any] = Field(default_factory=dict, description='JSON schema for the args object.')
    unattended_safe: bool = Field(description='May run without a person in safe_auto mode.')
    submits_applications: bool = False


# The tool catalogue the planner may use. The core branch implements these in
# agent/tools.py; names and safety flags here are binding.
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(name='status', description='Summarise tracker counts, queue and blockers.',
             unattended_safe=True),
    ToolSpec(name='discover', description='Discover new jobs from configured sources.',
             args_schema={'type': 'object', 'properties': {
                 'sources': {'type': 'array', 'items': {'type': 'string'}},
                 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}},
             unattended_safe=True),
    ToolSpec(name='score', description='Score unscored jobs against the candidate profile.',
             args_schema={'type': 'object', 'properties': {'limit': {'type': 'integer', 'minimum': 1}}},
             unattended_safe=True),
    ToolSpec(name='queue', description='Move qualified jobs into the application queue.',
             args_schema={'type': 'object', 'properties': {'limit': {'type': 'integer', 'minimum': 1}}},
             unattended_safe=True),
    ToolSpec(name='search_memory', description='Retrieve facts, resume chunks, jobs or past answers.',
             args_schema={'type': 'object', 'required': ['query'], 'properties': {
                 'query': {'type': 'string'}, 'k': {'type': 'integer', 'minimum': 1, 'maximum': 20},
                 'kinds': {'type': 'array', 'items': {'type': 'string'}}}},
             unattended_safe=True),
    ToolSpec(name='lookup_fact', description='Read one saved candidate fact by key.',
             args_schema={'type': 'object', 'required': ['key'], 'properties': {'key': {'type': 'string'}}},
             unattended_safe=True),
    ToolSpec(name='tailor_resume', description='Pick or tailor a resume for a job from source material.',
             args_schema={'type': 'object', 'required': ['job_id'], 'properties': {'job_id': {'type': 'integer'}}},
             unattended_safe=True),
    ToolSpec(name='apply_preview', description='List queued jobs that are ready to submit and why others are blocked.',
             args_schema={'type': 'object', 'properties': {'limit': {'type': 'integer', 'minimum': 1}}},
             unattended_safe=True),
    ToolSpec(name='apply', description='Submit applications through a validated adapter.',
             args_schema={'type': 'object', 'required': ['job_ids'], 'properties': {
                 'job_ids': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1}}},
             unattended_safe=False, submits_applications=True),
    ToolSpec(name='retry', description='Retry due temporary failures.',
             args_schema={'type': 'object', 'properties': {'limit': {'type': 'integer', 'minimum': 1}}},
             unattended_safe=False, submits_applications=True),
    ToolSpec(name='ask_user', description='Pause and ask the person a question.',
             args_schema={'type': 'object', 'required': ['question'], 'properties': {'question': {'type': 'string'}}},
             unattended_safe=True),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


class Step(BaseModel):
    id: str
    index: int = Field(ge=0)
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(description='Why the planner chose this step, shown to the user.')
    status: StepStatus = StepStatus.PENDING
    result_summary: str | None = None
    error: str | None = None
    needs_user_reason: str | None = None
    job_id: int | None = Field(default=None, description='Job the step is about, for UI links.')
    started_at: str | None = None
    finished_at: str | None = None
    log: list[str] = Field(default_factory=list)

    @field_validator('tool')
    @classmethod
    def _known_tool(cls, value: str) -> str:
        if value not in TOOLS_BY_NAME:
            raise ValueError(f'unknown tool: {value}')
        return value


class Plan(BaseModel):
    id: str
    goal: str = Field(min_length=1, max_length=2000)
    status: PlanStatus = PlanStatus.DRAFT
    mode: AgentMode = DEFAULT_AGENT_MODE
    steps: list[Step] = Field(default_factory=list)
    summary: str | None = Field(default=None, description='Planner explanation of the plan as a whole.')
    model: str | None = Field(default=None, description='LLM that produced the plan, or "stub".')
    replan_count: int = Field(default=0, ge=0, le=3)
    created_at: str
    updated_at: str


MAX_REPLANS = 3


class LLMProvider(str, Enum):
    OLLAMA = 'ollama'           # Local and free. Default.
    ANTHROPIC = 'anthropic'     # Paid API. Optional; needs allow_paid_llm and a key.


DEFAULT_LLM_PROVIDER = LLMProvider.OLLAMA
DEFAULT_OLLAMA_MODEL = 'qwen2.5:7b-instruct'
DEFAULT_EMBEDDER = 'local'      # sentence-transformers on this computer; no API cost.


class AgentSettings(BaseModel):
    """Agent settings. Defaults cost nothing to run: local Ollama and local embeddings."""

    mode: AgentMode = DEFAULT_AGENT_MODE
    llm_enabled: bool = False
    provider: LLMProvider = DEFAULT_LLM_PROVIDER
    model: str = DEFAULT_OLLAMA_MODEL
    embedder: str = DEFAULT_EMBEDDER
    allow_paid_llm: bool = Field(default=False, description='Must be true before a paid provider is used.')

    @model_validator(mode='after')
    def _paid_needs_opt_in(self) -> 'AgentSettings':
        if self.provider != LLMProvider.OLLAMA and not self.allow_paid_llm:
            raise ValueError('paid LLM providers need allow_paid_llm=true')
        return self


MEMORY_KINDS = ('fact', 'resume', 'job', 'answer', 'lesson')


class Snippet(BaseModel):
    """One retrieved memory item."""

    kind: str
    source_id: str
    text: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator('kind')
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in MEMORY_KINDS:
            raise ValueError(f'unknown memory kind: {value}')
        return value


class AnswerSuggestion(BaseModel):
    """A RAG-backed answer the user must confirm before it is used."""

    question: str
    answer: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Snippet] = Field(min_length=1)
    requires_confirmation: bool = True

    @field_validator('requires_confirmation')
    @classmethod
    def _always_confirm(cls, value: bool) -> bool:
        if not value:
            raise ValueError('RAG suggestions always require confirmation')
        return value


@runtime_checkable
class Retriever(Protocol):
    """Implemented by agent/memory. All methods must work offline."""

    def index_all(self, db: Any) -> int:
        """Build or refresh the index from the database and local files. Returns items indexed."""

    def index_item(self, kind: str, source_id: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        """Add or replace one item."""

    def retrieve(self, query: str, k: int = 5, kinds: list[str] | None = None) -> list[Snippet]:
        """Return up to k snippets, best first."""

    def suggest_answer(self, question: str, job_context: dict[str, Any] | None = None) -> AnswerSuggestion | None:
        """Suggest an answer with evidence, or None when memory has no support."""


class ChatReply(BaseModel):
    reply: str
    plan: Plan | None = None
    suggestions: list[AnswerSuggestion] = Field(default_factory=list)
