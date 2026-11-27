"""Create role-focused, ATS-readable PDFs from the candidate's saved resume.

Tailoring only reorders existing skills and achievements. It does not add
keywords or claims that do not already appear in the source resume.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAMILIES = {
    "backend": ("C#", ".NET", "ASP.NET", "REST API", "SQL", "Dapper", "microservices", "backend"),
    "fullstack": ("Angular", "React", "TypeScript", "frontend", "REST API", "SQL", "Java", "Spring"),
    "platform-sre": ("AWS", "EC2", "CloudWatch", "CI/CD", "GCP", "Docker", "production support", "reliability", "microservices"),
    "security": ("RBAC", "authorization", "access controls", "security", "role-", "validation", "middleware"),
}
HEADINGS = ("Professional Summary", "Technical Skills", "Work Experience", "Projects", "Education", "Achievements", "Certifications")
EMPLOYERS: tuple[str, ...] = ()
ROLE_PREFIXES: tuple[str, ...] = ()


def _clean(value: str) -> str:
    value = re.sub(r"\s+([,.;:])", r"\1", value)
    value = re.sub(r"\s+([)\]])", r"\1", value)
    value = re.sub(r"([(\[])\s+", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _extract_lines(source: Path) -> list[str]:
    node = shutil.which("node")
    extractor = ROOT / "scripts" / "extract_resume.mjs"
    if not node or not extractor.is_file():
        raise RuntimeError("Node.js and JobPilot's PDF.js package are required to tailor the saved resume")
    result = subprocess.run([node, str(extractor), str(source)], cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", timeout=90, check=True)
    lines = [_clean(line) for line in json.loads(result.stdout).get("lines", [])]
    if not any(line == "Professional Summary" for line in lines):
        raise ValueError("The saved resume could not be read as structured text; no tailored file was written")
    return [line for line in lines if line]


def _bullet_groups(lines: list[str], prefix: str) -> list[str]:
    groups: list[str] = []
    for line in lines:
        if line.startswith(prefix):
            groups.append(line[len(prefix):].strip())
        elif groups:
            groups[-1] += " " + line
    return groups


def _parse(lines: list[str], years: float) -> dict[str, object]:
    sections: dict[str, list[str]] = {heading: [] for heading in HEADINGS}
    current = ""
    contact: list[str] = []
    summary: list[str] = []
    for line in lines:
        if line in HEADINGS:
            current = line
            continue
        if not current:
            contact.append(line)
        else:
            sections[current].append(line)
    summary = sections["Professional Summary"]
    if not summary or not sections["Technical Skills"] or not sections["Work Experience"]:
        raise ValueError("The saved resume is missing a required section")
    summary_text = " ".join(summary)
    summary_text = re.sub(r"\b2\.5\+\s+years?\b", f"{years:g} years", summary_text, flags=re.I)
    skills = _bullet_groups(sections["Technical Skills"], "•")

    work: list[dict[str, object]] = []
    active: dict[str, object] | None = None
    for line in sections["Work Experience"]:
        if any(line.startswith(employer) for employer in EMPLOYERS):
            active = {"company": line, "role": "", "bullets": []}
            work.append(active)
        elif any(line.startswith(role) for role in ROLE_PREFIXES):
            if active is None:
                active = {"company": "", "role": "", "bullets": []}
                work.append(active)
            active["role"] = line
        elif line.startswith("–"):
            if active is None:
                active = {"company": "", "role": "", "bullets": []}
                work.append(active)
            active["bullets"].append(line.lstrip("– ").strip())
        elif active and active["bullets"]:
            active["bullets"][-1] += " " + line
        elif active:
            active["role"] = (str(active["role"]) + " " + line).strip()
    return {"contact": contact, "summary": summary_text, "skills": skills, "work": work,
            "projects": _bullet_groups(sections["Projects"], "•"),
            "education": sections["Education"],
            "achievements": _bullet_groups(sections["Achievements"], "•"),
            "certifications": _bullet_groups(sections["Certifications"], "•")}


def _rank(items: list[str], family: str) -> list[str]:
    terms = FAMILIES[family]
    ranked = sorted(enumerate(items), key=lambda pair: (-sum(pair[1].casefold().count(term.casefold()) for term in terms), pair[0]))
    return [item for _, item in ranked]


_STOP_WORDS = {
    "about", "across", "after", "also", "and", "any", "are", "as", "at", "be", "best", "build",
    "building", "by", "can", "code", "develop", "development", "experience", "for", "from", "have",
    "in", "including", "into", "is", "job", "maintain", "must", "of", "on", "or", "our", "role",
    "should", "software", "strong", "that", "the", "their", "this", "to", "using", "with", "work",
}


@lru_cache(maxsize=32)
def _description_frequencies(description: str) -> dict[str, int]:
    frequencies: dict[str, int] = {}
    for term in re.findall(r"[a-z0-9+#.]+", description.casefold()):
        if term not in _STOP_WORDS and (len(term) > 2 or term in {"c#", "c++", ".net", "go"}):
            frequencies[term] = min(3, frequencies.get(term, 0) + 1)

    return frequencies


def _description_score(item: str, description: str) -> int:
    """Rank only words and technologies already present in a resume item."""
    source_terms = set(re.findall(r"[a-z0-9+#.]+", item.casefold()))
    frequencies = _description_frequencies(description)
    score = sum(weight for term, weight in frequencies.items() if term in source_terms)
    # Reward a direct phrase match so compound skills such as ASP.NET Core rank
    # above a coincidental match on the word "core".
    phrase = _clean(item).casefold()
    if len(phrase) >= 4 and phrase in description.casefold():
        score += 4
    return score


def _rank_for_description(items: list[str], description: str) -> list[str]:
    ranked = sorted(enumerate(items), key=lambda pair: (-_description_score(pair[1], description), pair[0]))
    return [item for _, item in ranked]


def _rank_skill_groups(items: list[str], description: str) -> list[str]:
    """Prioritize matching skill categories and skills without adding claims."""
    groups: list[tuple[int, int, str]] = []
    for index, item in enumerate(items):
        match = re.match(r"^(\s*•\s*)([^:]+):(.*)$", item)
        if not match:
            groups.append((_description_score(item, description), index, item))
            continue
        prefix, category, values = match.groups()
        skills = [part.strip() for part in values.split(",") if part.strip()]
        ordered = sorted(enumerate(skills), key=lambda pair: (-_description_score(pair[1], description), pair[0]))
        category_score = sum(_description_score(skill, description) for skill in skills)
        rendered = prefix + category.strip() + ": " + ", ".join(skill for _, skill in ordered)
        groups.append((category_score, index, rendered))
    groups.sort(key=lambda row: (-row[0], row[1]))
    return [item for _, _, item in groups]


def _document(model: dict[str, object], family: str | None, description: str | None = None) -> str:
    esc = lambda value: html.escape(str(value), quote=True)
    if description:
        skill_groups = _rank_skill_groups(list(model["skills"]), description)
        rank_items = lambda items: _rank_for_description(items, description)
    else:
        skill_groups = _rank(list(model["skills"]), family or "backend")
        rank_items = lambda items: _rank(items, family or "backend")
    work_html = []
    for employer in model["work"]:
        bullets = rank_items(list(employer["bullets"]))
        work_html.append(f"<h3>{esc(employer['company'])}</h3><p class='role'>{esc(employer['role'])}</p><ul>" + "".join(f"<li>{esc(item)}</li>" for item in bullets) + "</ul>")
    def section(title: str, body: str) -> str:
        return f"<section><h2>{title}</h2>{body}</section>"
    name = model["contact"][0] if model["contact"] else "Candidate"
    contact = " · ".join(model["contact"][1:])
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
@page{{size:A4;margin:14mm 16mm}}*{{box-sizing:border-box}}body{{font:9.5pt Arial,Helvetica,sans-serif;color:#15202b;line-height:1.32;margin:0}}header{{border-bottom:1.5px solid #263a50;padding-bottom:8px;margin-bottom:10px}}h1{{font-size:21pt;margin:0 0 4px}}.contact{{font-size:8.5pt;color:#35465a}}h2{{font-size:11pt;text-transform:uppercase;letter-spacing:.5px;margin:10px 0 4px;border-bottom:1px solid #cbd5df;padding-bottom:2px}}h3{{font-size:9.5pt;margin:7px 0 1px}}p{{margin:3px 0}}.role{{font-weight:bold;color:#37465a}}ul{{padding-left:17px;margin:3px 0 5px}}li{{margin:0 0 3px;break-inside:avoid}}section{{break-inside:auto}}.skill{{margin:2px 0}}
</style></head><body><header><h1>{esc(name)}</h1><div class='contact'>{esc(contact)}</div></header>
{section('Professional Summary', f'<p>{esc(model["summary"])}</p>')}
{section('Technical Skills', ''.join(f'<p class="skill">{esc(item)}</p>' for item in skill_groups))}
{section('Work Experience', ''.join(work_html))}
{section('Projects', '<ul>'+''.join(f'<li>{esc(item)}</li>' for item in rank_items(list(model['projects'])))+'</ul>')}
{section('Education', '<p>'+'<br>'.join(esc(x) for x in model['education'])+'</p>')}
{section('Achievements', '<ul>'+''.join(f'<li>{esc(item)}</li>' for item in model['achievements'])+'</ul>')}
{section('Certifications', '<ul>'+''.join(f'<li>{esc(item)}</li>' for item in model['certifications'])+'</ul>')}
</body></html>"""


def generate(data_root: Path | None = None, years: float = 3.0, force: bool = False) -> dict[str, str]:
    root = (data_root or ROOT / "data").resolve()
    resume_dir = root / "resumes"
    source = resume_dir / "generic.pdf"
    if not source.is_file() or source.stat().st_size < 1000:
        raise RuntimeError("The saved source resume is missing or too small to read")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = resume_dir / ".tailored-manifest.json"
    output = {family: resume_dir / f"tailored-{family}.pdf" for family in FAMILIES}
    if not force and manifest.is_file() and all(path.is_file() and path.stat().st_size > 1000 for path in output.values()):
        try:
            if json.loads(manifest.read_text(encoding="utf-8")).get("source_sha256") == digest:
                return {family: path.name for family, path in output.items()}
        except (OSError, ValueError):
            pass
    model = _parse(_extract_lines(source), years)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page()
            for family, path in output.items():
                page.set_content(_document(model, family), wait_until="load")
                page.pdf(path=str(path), format="A4", print_background=True,
                         margin={"top":"0mm","right":"0mm","bottom":"0mm","left":"0mm"})
                if not path.is_file() or path.stat().st_size < 1000:
                    raise RuntimeError(f"Resume generation failed for {family}")
        finally:
            browser.close()
    manifest.write_text(json.dumps({"source_sha256": digest, "experience_years": years,
                                    "generated": {family: path.name for family, path in output.items()}}, indent=2), encoding="utf-8")
    return {family: path.name for family, path in output.items()}


def generate_for_job(job_id: int, description: str, data_root: Path | None = None,
                     years: float = 3.0) -> str:
    """Create a cached job-specific PDF by reordering the saved resume's evidence."""
    if int(job_id) < 1:
        raise ValueError("A saved job ID is required for a job-specific resume")
    description = _clean(description)
    if len(description) < 40:
        raise ValueError("The job description is too short to tailor a resume")
    root = (data_root or ROOT / "data").resolve()
    resume_dir = root / "resumes"
    source = resume_dir / "generic.pdf"
    if not source.is_file() or source.stat().st_size < 1000:
        raise RuntimeError("The saved source resume is missing or too small to read")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    description_digest = hashlib.sha256(f"{description.casefold()}|experience:{float(years):g}".encode("utf-8")).hexdigest()
    target = resume_dir / f"tailored-job-{int(job_id)}-{source_digest[:10]}-{description_digest[:10]}.pdf"
    if target.is_file() and target.stat().st_size > 1000:
        return target.name

    model = _parse(_extract_lines(source), years)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page()
            page.set_content(_document(model, None, description), wait_until="load")
            page.pdf(path=str(target), format="A4", print_background=True,
                     margin={"top":"0mm","right":"0mm","bottom":"0mm","left":"0mm"})
        finally:
            browser.close()
    if not target.is_file() or target.stat().st_size < 1000:
        raise RuntimeError("Job-specific resume generation failed")
    return target.name
