# CareerOS

**A local-first job search and application operations agent.** CareerOS gathers public job listings, ranks them against a candidate profile, helps tailor application materials, and keeps the review and application workflow in one local dashboard.

It is designed to make a job search more organized and less repetitive while keeping candidate data on the user's own computer and preserving human review around application decisions.

## What it does

- **Discovers roles:** reads configured public employer career boards and supported remote-job sources, deduplicating listings into a local tracker.
- **Matches jobs to a profile:** scores role descriptions against candidate skills, preferences, location rules, and eligibility settings.
- **Prepares applications:** selects or tailors a resume from user-provided source material and maps application questions only to saved candidate facts.
- **Supports careful form workflows:** fills supported ATS forms and records screenshots, missing answers, attempt history, and submission receipts. Automation is limited to explicitly enabled, validated adapters; unsupported flows remain for review.
- **Tracks the search:** a local dashboard and SQLite database show discovery runs, job status, application queue, evidence, and exportable CSV history.

## How it works

```text
Public job sources → discovery and deduplication → profile matching → review queue
                                                              ↓
                            resume and answer preparation → supported ATS workflow
                                                              ↓
                                      local receipts, history, and CSV export
```

The dashboard runs on your computer at `http://127.0.0.1:8765`. The application database, browser sessions, resumes, screenshots, and logs stay in the local `data/` directory.

## AI and agentic workflow

CareerOS currently uses a **deterministic, evidence-based workflow**, rather than an LLM-driven reasoning loop:

- **Agentic orchestration:** a local runtime coordinates bounded discovery, scoring, queue, and application-worker steps; it records state and receipts, and pauses when a person must review or resolve a blocker.
- **Job matching:** weighted skill and role matches, plus explicit location, experience, and eligibility rules, produce an explainable score breakdown.
- **Candidate-fact grounding:** saved profile facts and answers are retrieved from local configuration and SQLite-backed candidate data. Unknown answers are meant to remain unresolved instead of being invented.
- **Application preparation:** resume tailoring reorders content extracted from the user's source resume; it does not intentionally add new claims. Supported browser adapters handle defined ATS forms and report outcomes.

**LLM:** no LLM provider or model is required or used for the matching and answer-mapping paths described here. The example settings keep `llmEnabled` off.

**RAG:** this project does not implement vector embeddings, a vector database, or LLM retrieval-augmented generation. It uses structured local fact lookup and deterministic matching, which should not be labelled RAG.


## Safety and limitations

- New installations start with unattended submission **off** and no ATS adapters allowlisted. Review the settings and adapter behavior before enabling any automation.
- CareerOS does not bypass CAPTCHAs, one-time codes, login controls, or access restrictions. Those flows pause for a person or remain manual.
- A submission is counted as applied only after the configured adapter observes a confirmation. Ambiguous attempts are left for review to reduce duplicate submissions.
- Saved answers should be accurate and based on facts you provide. Unknown eligibility, employment, or experience answers are left unresolved rather than guessed.
- Job-board APIs and page layouts can change. Source coverage and form compatibility vary by employer and are not guaranteed.
- This is a personal, local-first project, not an official job-board integration or a guarantee of employment outcomes.

## Requirements

- Windows 10/11 (the included launcher and sign-in task are Windows-specific)
- Python 3.11 or newer
- Node.js and npm for resume PDF tooling
- Google Chrome for browser workflows

## Quick start (Windows)

1. Clone the repository and open PowerShell in the project directory.
2. Create and activate a virtual environment, then install the agent dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements-agent.txt
   npm install
   ```

3. Copy the example configuration files to the names used by the app:

   ```powershell
   Get-ChildItem config\*.example.json | ForEach-Object {
     $destination = $_.Name -replace '\.example(?=\.json$)', ''
     Copy-Item $_.FullName (Join-Path $_.DirectoryName $destination)
   }
   ```

4. Add your own details to `config/profile.json`, `config/eligibility.json`, `config/skills.json`, and `config/answers.json`. Add your source resume as `data/resumes/generic.pdf`. The app derives role-specific versions from this file without adding new skills or achievements. Do not add personal files to Git.
5. Initialize the local database and start the dashboard:

   ```powershell
   python jobpilot.py init
   .\run-agent.cmd
   ```

6. Open `http://127.0.0.1:8765`. Start with discovery and matching; review each application before deciding whether to enable any supported submission adapter.

For CLI help and other workflows, run `python jobpilot.py --help`. `python jobpilot.py doctor` checks the local setup. `python jobpilot.py status` shows the current tracker state.

## Configuration

Examples live in `config/*.example.json`. Copy them to the corresponding local filenames before first run. Add public employer board definitions in `config/companies.json`, and keep `autoSubmit` and `unattended` disabled until you have reviewed the supported adapters and their confirmation behavior.

The local `data/` directory contains the SQLite database, browser profiles, resumes, exports, screenshots, run history, and logs. It is excluded from Git. Never commit real candidate data, portal credentials, access tokens, or application artifacts.

## Architecture

- **Python:** discovery, matching, candidate knowledge, application orchestration, FastAPI dashboard, and SQLite persistence.
- **Playwright:** browser automation for supported, user-authorized workflows.
- **HTML/CSS:** local dashboard interface.
- **Node.js + `pdfjs-dist`:** resume text extraction and PDF tooling.

## Development

Install development dependencies with `python -m pip install -r requirements-dev.txt`. The repository includes unit and offline browser-fixture tests under `tests/`; live employer applications are not a safe test target.

## Project name

**CareerOS** describes the project as career search tooling for organizing role discovery, preparing applications, and tracking outcomes.

