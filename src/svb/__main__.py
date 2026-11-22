"""svb - recommend a semantic version bump from a git commit range."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, NamedTuple, Optional

import typer
from openai import OpenAI

DEFAULT_TO = "HEAD"
MAX_DIFF_CHARS = 15_000
MAX_COMMITS_IN_PROMPT = 200
MAX_COMMIT_BODY_CHARS = 1_000

MODELS = {
    "OPENROUTER_API_KEY": "deepseek/deepseek-chat-v3-0324:free",
    "GEMINI_API_KEY": "gemini-2.0-flash",
    "GROQ_API_KEY": "llama-3.3-70b-versatile",
}

PROVIDERS = {
    "OPENROUTER_API_KEY": ("OpenRouter", "https://openrouter.ai/api/v1"),
    "GEMINI_API_KEY": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    "GROQ_API_KEY": ("Groq", "https://api.groq.com/openai/v1"),
}

SKIP_FILENAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "go.sum"}
SKIP_DIRS = {"node_modules", "dist", "build", ".git", "vendor", "coverage"}
SKIP_SUFFIXES = (".min.js", ".min.css")
SKIP_EXTENSIONS = {
    ".a", ".avi", ".bin", ".bmp", ".bz2", ".class", ".db", ".dll", ".dylib", ".eot", ".exe",
    ".flac", ".gif", ".gz", ".ico", ".jar", ".jpeg", ".jpg", ".mkv", ".mov", ".mp3", ".mp4",
    ".o", ".ogg", ".otf", ".pdf", ".png", ".psd", ".pyc", ".rar", ".so", ".sqlite", ".tar",
    ".tif", ".tiff", ".ttf", ".wav", ".war", ".webm", ".webp", ".woff", ".woff2", ".xz",
    ".zip", ".7z",
}

SYSTEM_PROMPT = """You are an expert software release engineer applying Semantic Versioning 2.0.0.

Given a git commit range (commit metadata and diffs), recommend exactly one version bump.

Rules:
- MAJOR: incompatible or breaking changes to a public API, or removal of existing functionality.
- MINOR: new functionality added in a backward-compatible way.
- PATCH: backward-compatible bug fixes, refactors, performance improvements, chores, docs, tests, or build/tooling changes.
- If changes are mixed, choose the highest-impact bump.
- For pre-1.0 versions (0.y.z), breaking changes may reasonably map to MINOR, but use your judgement and mention it briefly in the reasoning.

Return STRICT JSON only, with no markdown, no code fences, and no commentary outside the JSON object.
The JSON object must have exactly this shape:
{"bump": "major|minor|patch", "reasoning": "one paragraph", "confidence": 0.0}

"confidence" must be a number from 0.0 to 1.0."""


class SvbError(Exception):
    """A user-facing error."""


class ParseError(SvbError):
    """The model response could not be parsed as the required JSON."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


class Commit(NamedTuple):
    sha: str
    short: str
    author: str
    date: str
    subject: str
    body: str


class FileStat(NamedTuple):
    path: str
    added: Optional[int]
    deleted: Optional[int]


class Analysis(NamedTuple):
    bump: str
    reasoning: str
    confidence: float


def run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise SvbError("git was not found. Install git and try again.") from exc

    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown git error"
        raise SvbError(f"git {' '.join(args)} failed: {detail}")
    return proc


def ensure_git_repo() -> None:
    proc = run_git(["rev-parse", "--is-inside-work-tree"], check=False)
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        raise SvbError("Not inside a git repository. Run svb from a Git working tree.")


def resolve_commit(rev: str, label: str) -> str:
    proc = run_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], check=False)
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise SvbError(f"{label} '{rev}' does not resolve to a commit.")
    return sha


def range_count(from_sha: str, to_sha: str) -> int:
    out = run_git(["rev-list", "--count", f"{from_sha}..{to_sha}"]).stdout.strip()
    return int(out or "0")


def commit_subject(sha: str) -> str:
    subject = run_git(["log", "-1", "--format=%s", sha]).stdout.strip()
    return subject or "(no subject)"


def read_commits(from_sha: str, to_sha: str) -> list[Commit]:
    fmt = "%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%b%x1e"
    raw = run_git(["log", "--reverse", f"--format={fmt}", f"{from_sha}..{to_sha}"]).stdout
    commits: list[Commit] = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 6:
            continue
        sha, short, author, date, subject, body = parts[:6]
        body = body.strip()
        if len(body) > MAX_COMMIT_BODY_CHARS:
            body = body[:MAX_COMMIT_BODY_CHARS].rstrip() + "\n... [body truncated]"
        commits.append(Commit(sha, short, author, date, subject, body))
    return commits


def read_numstat(from_sha: str, to_sha: str) -> list[FileStat]:
    out = run_git(
        ["diff", "--numstat", "-z", "--no-renames", f"{from_sha}..{to_sha}"]
    ).stdout
    stats: list[FileStat] = []
    for record in out.split("\0"):
        if not record:
            continue
        parts = record.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        stats.append(FileStat(path, None if added == "-" else int(added), None if deleted == "-" else int(deleted)))
    return stats


def skip_reason(path: str) -> Optional[str]:
    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return "empty path"

    basename = parts[-1].lower()
    if any(part.lower() in SKIP_DIRS for part in parts[:-1]):
        return "generated or vendored directory"
    if basename in SKIP_FILENAMES:
        return "lockfile"
    if basename.endswith(SKIP_SUFFIXES):
        return "minified file"
    if os.path.splitext(basename)[1] in SKIP_EXTENSIONS:
        return "binary or media file"
    return None


def read_diff(
    from_sha: str, to_sha: str, stats: list[FileStat]
) -> tuple[str, list[tuple[str, str]], int, bool]:
    skipped: list[tuple[str, str]] = []
    candidates: list[FileStat] = []

    for stat in stats:
        reason = skip_reason(stat.path)
        if reason:
            skipped.append((stat.path, reason))
            continue
        if stat.added is None or stat.deleted is None:
            skipped.append((stat.path, "binary file"))
            continue
        if stat.added + stat.deleted > MAX_DIFF_CHARS:
            skipped.append((stat.path, "large change set"))
            continue
        candidates.append(stat)

    candidates.sort(key=lambda item: ((item.added or 0) + (item.deleted or 0), item.path))

    chunks: list[str] = []
    budget = MAX_DIFF_CHARS
    included = 0
    truncated = False

    for stat in candidates:
        if budget <= 0:
            skipped.append((stat.path, "diff budget exhausted"))
            continue

        patch = run_git(
            ["diff", "--no-color", "--no-ext-diff", "--no-renames", f"{from_sha}..{to_sha}", "--", f":(literal){stat.path}"]
        ).stdout
        if not patch.strip():
            continue

        if len(patch) > budget:
            marker = "\n... [diff truncated to fit budget]\n"
            allowed = max(0, budget - len(marker))
            patch = patch[:allowed].rstrip() + marker
            truncated = True

        chunks.append(patch if patch.endswith("\n") else patch + "\n")
        budget -= len(patch)
        included += 1

    return "".join(chunks), skipped, included, truncated


def build_prompt(
    from_sha: str,
    to_sha: str,
    total_commits: int,
    commits: list[Commit],
    stats: list[FileStat],
    diff_text: str,
    skipped: list[tuple[str, str]],
    included_count: int,
    truncated: bool,
) -> str:
    insertions = sum(stat.added or 0 for stat in stats)
    deletions = sum(stat.deleted or 0 for stat in stats)

    lines = [
        f"Range: {from_sha}..{to_sha}",
        f"Commits in range: {total_commits}",
        f"Files changed: {len(stats)}",
        f"Insertions: {insertions}",
        f"Deletions: {deletions}",
        "",
        "=== COMMIT METADATA (oldest first) ===",
    ]

    shown = commits[-MAX_COMMITS_IN_PROMPT:]
    omitted = len(commits) - len(shown)
    if omitted > 0:
        lines.append(
            f"[Showing the most recent {len(shown)} commits; {omitted} older commits omitted.]"
        )

    for commit in shown:
        lines.append(f"{commit.short} {commit.date} {commit.author}")
        lines.append(commit.subject)
        if commit.body:
            lines.append(commit.body)
        lines.append("")

    lines.append("=== DIFF ===")
    if truncated:
        lines.append(f"[Diff truncated to {MAX_DIFF_CHARS} characters.]")
    if diff_text:
        lines.append(diff_text)
    else:
        lines.append("[No textual diff content available.]")

    lines.append("=== SKIPPED OR OMITTED FILES ===")
    if skipped:
        for path, reason in skipped:
            lines.append(f"- {path} ({reason})")
    else:
        lines.append("None.")

    lines.append(f"\nTextual diffs included for {included_count} file(s).")
    return "\n".join(lines)


def select_provider() -> tuple[str, str]:
    for env_var in MODELS:
        api_key = os.environ.get(env_var)
        if api_key:
            return env_var, api_key
    env_vars = ", ".join(MODELS)
    raise SvbError(f"No API key found. Set one of: {env_vars}.")


def call_model(env_var: str, api_key: str, model: str, messages: list[dict[str, str]]) -> str:
    provider_name, default_base_url = PROVIDERS[env_var]
    base_url = os.environ.get("SVB_BASE_URL") or default_base_url
    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0, max_retries=0)
        response = client.chat.completions.create(model=model, messages=messages, temperature=0)
    except Exception as exc:
        raise SvbError(f"{provider_name} request failed: {exc}") from exc

    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise SvbError(f"{provider_name} returned an empty response.")
    return content


def parse_analysis(raw: str) -> Analysis:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found")

    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")

    bump = str(data.get("bump", "")).strip().lower()
    if bump not in {"major", "minor", "patch"}:
        raise ValueError("bump must be one of major, minor, patch")

    reasoning = str(data.get("reasoning", "")).strip()
    if not reasoning:
        raise ValueError("reasoning is missing")

    confidence = float(data.get("confidence"))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")

    return Analysis(bump=bump, reasoning=reasoning, confidence=confidence)


def analyze(prompt: str, model_override: Optional[str]) -> Analysis:
    env_var, api_key = select_provider()
    model = model_override or MODELS[env_var]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    raw = call_model(env_var, api_key, model, messages)
    try:
        return parse_analysis(raw)
    except (ValueError, TypeError):
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    "Your previous response was not valid JSON. Respond with ONLY a single "
                    'JSON object and no markdown fences or commentary. Required shape: '
                    '{"bump": "major|minor|patch", "reasoning": "one paragraph", '
                    '"confidence": 0.0}'
                ),
            },
        ]
        raw = call_model(env_var, api_key, model, retry_messages)
        try:
            return parse_analysis(raw)
        except (ValueError, TypeError) as exc:
            raise ParseError(
                f"Could not parse the model response as JSON: {exc}", raw
            ) from exc


def parse_version(current: str) -> tuple[int, int, int]:
    parts = current.strip().split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise SvbError(f"--current must look like x.y.z (got '{current}').")
    return int(parts[0]), int(parts[1]), int(parts[2])


def bump_version(current: str, bump: str) -> str:
    major, minor, patch = parse_version(current)
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


app = typer.Typer(
    add_completion=False,
    help="Recommend a semantic version bump from a git commit range.",
)


@app.command()
def main(
    from_: str = typer.Option(..., "--from", help="Start commit or tag (exclusive)."),
    to: str = typer.Option(DEFAULT_TO, "--to", help="End commit or tag (inclusive). Defaults to HEAD."),
    current: Optional[str] = typer.Option(None, "--current", help="Current version (x.y.z) used to calculate the target."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON only."),
    model: Optional[str] = typer.Option(None, "--model", help="Override the provider's default model."),
) -> None:
    try:
        ensure_git_repo()
        from_sha = resolve_commit(from_, "--from")
        to_sha = resolve_commit(to, "--to")
        total_commits = range_count(from_sha, to_sha)
        if total_commits == 0:
            raise SvbError(f"Commit range {from_}..{to} is empty (exclusive of --from).")

        if current is not None:
            parse_version(current)

        commits = read_commits(from_sha, to_sha)
        stats = read_numstat(from_sha, to_sha)
        diff_text, skipped, included_count, truncated = read_diff(from_sha, to_sha, stats)
        prompt = build_prompt(
            from_sha, to_sha, total_commits, commits, stats, diff_text, skipped, included_count, truncated
        )
        analysis = analyze(prompt, model)

        target_version = bump_version(current, analysis.bump) if current is not None else None

        if json_output:
            payload: dict[str, Any] = {
                "bump": analysis.bump,
                "reasoning": analysis.reasoning,
                "confidence": analysis.confidence,
                "from": from_sha,
                "to": to_sha,
                "commits": total_commits,
                "files_changed": len(stats),
                "target_version": target_version,
            }
            typer.echo(json.dumps(payload, ensure_ascii=False))
            return

        insertions = sum(stat.added or 0 for stat in stats)
        deletions = sum(stat.deleted or 0 for stat in stats)
        typer.echo(f"From:   {from_sha[:7]} ({commit_subject(from_sha)})")
        typer.echo(f"To:     {to_sha[:7]} ({commit_subject(to_sha)})")
        typer.echo(
            f"Commits: {total_commits}   Files: {len(stats)}   +{insertions}/-{deletions}"
        )
        typer.echo("")
        typer.echo(f"Recommended bump: {analysis.bump.upper()}")
        typer.echo(f"Reasoning: {analysis.reasoning}")
        if target_version is not None:
            typer.echo(f"Target version: {target_version}")
    except ParseError as exc:
        typer.echo(f"Error: {exc}", err=True)
        typer.echo("Raw model response:", err=True)
        typer.echo(exc.raw, err=True)
        raise typer.Exit(1) from exc
    except SvbError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
