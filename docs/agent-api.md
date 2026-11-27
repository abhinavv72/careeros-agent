# Agent API (contract version 1)

The agent layer adds routes under `/api/agent`, served by `agent_server.py` at `http://127.0.0.1:8765`. The shapes come from `agent/contracts.py`; that file is the source of truth if this page and the code ever differ.

**Current state: stub.** `agent_api.py` returns fixture data from memory and adds the header `X-Agent-Stub: true` to every response. It never runs discovery or submits anything. The agent core will replace the stub with the real planner and executor and keep the same routes and shapes.

## Zero-cost defaults

- Planner LLM: **Ollama** on this computer (`qwen2.5:7b-instruct` by default). Free.
- Embeddings: **local** sentence-transformers model, with BM25 keyword search as a fallback when it is not installed. Free.
- The Claude API is an optional provider. It is used only when `provider` is `anthropic` **and** `allow_paid_llm` is `true`; the settings model rejects anything else.

## Rules every caller must follow

- Same local-only rules as the rest of the server: loopback host only, and every `POST`/`PUT` must send the header `X-Jobpilot-Request: local-dashboard`.
- Tools with `submits_applications: true` are never `unattended_safe`. In every mode, a submission still obeys `autoSubmit`, `unattended` and `allowedAutoSubmitAts` in `config/settings.json`.
- `AnswerSuggestion` always has at least one evidence snippet and `requires_confirmation: true`. The UI must show it as "Suggested, confirm before use".

## Types

| Type | Fields |
|---|---|
| `AgentMode` | `off`, `plan_only`, `safe_auto` (default), `full` |
| `PlanStatus` | `draft`, `running`, `paused`, `needs_user`, `done`, `failed`, `cancelled` |
| `StepStatus` | `pending`, `running`, `done`, `failed`, `needs_user`, `skipped` |
| `ToolSpec` | `name`, `description`, `args_schema`, `unattended_safe`, `submits_applications` |
| `Step` | `id`, `index`, `tool`, `args`, `reason`, `status`, `result_summary`, `error`, `needs_user_reason`, `job_id`, `started_at`, `finished_at`, `log[]` |
| `Plan` | `id`, `goal`, `status`, `mode`, `steps[]`, `summary`, `model`, `replan_count` (max 3), `created_at`, `updated_at` |
| `AgentSettings` | `mode`, `llm_enabled`, `provider` (`ollama` or `anthropic`), `model`, `embedder`, `allow_paid_llm` |
| `Snippet` | `kind` (`fact`, `resume`, `job`, `answer`, `lesson`), `source_id`, `text`, `score`, `metadata` |
| `AnswerSuggestion` | `question`, `answer`, `confidence` (0 to 1), `evidence[]` (at least 1), `requires_confirmation` (always true) |
| `ChatReply` | `reply`, `plan` (optional), `suggestions[]` |

Timestamps are ISO 8601 UTC strings.

## Routes

| Method and path | Body | Returns | Notes |
|---|---|---|---|
| `GET /api/agent/tools` | | `ToolSpec[]` | The tools the planner may use. |
| `GET /api/agent/settings` | | `AgentSettings` | |
| `PUT /api/agent/settings` | `{"mode": AgentMode}` | `AgentSettings` | 422 on an unknown mode. |
| `POST /api/agent/plan` | `{"goal": str}` | `Plan` (status `draft`) | Nothing runs until approved. |
| `GET /api/agent/plans?limit=20` | | `Plan[]` | Newest first. |
| `GET /api/agent/plans/{id}` | | `Plan` | Poll every 2 seconds while `running`. 404 if missing. |
| `POST /api/agent/plans/{id}/approve` | | `Plan` | Starts a draft or paused plan, or approves the step that is `needs_user`. 409 if the plan is finished or the mode is `off`. |
| `POST /api/agent/plans/{id}/pause` | | `Plan` | 409 if finished. |
| `POST /api/agent/plans/{id}/cancel` | | `Plan` | Remaining steps become `skipped`. |
| `POST /api/agent/chat` | `{"message": str}` | `ChatReply` | May include a draft plan or answer suggestions. |
| `GET /api/agent/memory/search?q=&k=5` | | `Snippet[]` | |

## Stub behaviour (for UI work)

`POST /plan` always returns the same six steps: `status`, `discover`, `score`, `queue`, `apply_preview`, `apply`. After approve, each `GET /plans/{id}` moves the plan forward one step. When it reaches `apply` the step becomes `needs_user` with a reason and a `job_id`, and the plan becomes `needs_user`. Approving again finishes it without submitting anything. Chat returns a plan for messages containing "find", "apply to" or starting with "plan ", and an answer suggestion for messages about "notice".

## Example

```json
{
  "id": "3f9c0a1b2d4e",
  "goal": "apply to 5 remote backend roles",
  "status": "needs_user",
  "mode": "safe_auto",
  "model": "stub",
  "summary": "Stub plan: discover, score and queue on their own, then ask before submitting.",
  "replan_count": 0,
  "steps": [
    {"id": "3f9c0a1b2d4e-5", "index": 5, "tool": "apply", "args": {"job_ids": [101, 102]},
     "reason": "Submit the ready roles through validated adapters.", "status": "needs_user",
     "needs_user_reason": "Submitting applications needs your approval.", "job_id": 101,
     "result_summary": null, "error": null, "started_at": null, "finished_at": null, "log": []}
  ],
  "created_at": "2026-11-27T18:00:00+00:00",
  "updated_at": "2026-11-27T18:00:10+00:00"
}
```
