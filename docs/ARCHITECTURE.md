# Architecture

`svb` is a single-purpose command-line tool. This document explains its data flow, the important design decisions, and the boundaries that keep the tool small.

## Data flow

```text
CLI arguments
    |
    v
Git validation and range resolution
    |
    v
Commit metadata + diff stats + filtered diff content
    |
    v
Prompt construction
    |
    v
Provider selection + OpenAI-compatible chat request
    |
    v
Strict JSON parsing (one retry on failure)
    |
    v
Human-readable or JSON output
```

## Code map

All runtime logic lives in `src/svb/__main__.py`.

| Area | Functions |
| --- | --- |
| CLI | `main`, Typer `app` |
| Git access | `run_git`, `ensure_git_repo`, `resolve_commit`, `range_count`, `commit_subject`, `read_commits`, `read_numstat`, `read_diff` |
| Filtering | `skip_reason`, skip constants |
| Prompt | `build_prompt`, `SYSTEM_PROMPT` |
| Providers | `MODELS`, `BASE_URLS`, `PROVIDER_NAMES`, `select_provider`, `call_model` |
| Parsing | `parse_analysis`, `analyze` |
| Versioning | `parse_version`, `bump_version` |
| Errors | `SvbError`, `ParseError` |

## Git collection

`svb` shells out to `git` with `subprocess`. It does not use GitPython or another Git library. This keeps the dependency set small and makes the binary easier to build.

### Validation and range resolution

1. `git rev-parse --is-inside-work-tree` confirms the command is running inside a Git working tree.
2. `git rev-parse --verify --quiet <rev>^{commit}` resolves `--from` and `--to` to commit SHAs. Annotated tags are peeled to commits.
3. `git rev-list --count <from>..<to>` determines whether the range contains any commits. `--from` is exclusive, `--to` is inclusive.

Empty ranges are rejected before any AI call.

### Commit metadata

`git log --reverse` is used so the prompt presents commits oldest-first. Each record contains:

- full SHA
- short SHA
- author name
- author date (ISO 8601)
- subject
- body

Fields are separated with ASCII unit separators (`\x1f`) and records with ASCII record separators (`\x1e`) to avoid ambiguity with normal commit text. Bodies longer than 1,000 characters are truncated per commit.

The prompt includes at most the most recent 200 commits. The reported `commits` count is always the true count from `git rev-list --count`, so truncation of the prompt does not change the output contract.

### Diff stats

`git diff --numstat -z --no-renames <from>..<to>` provides one record per changed file with added/deleted line counts. The `-z` option avoids quoting and path-parsing problems; `--no-renames` turns renames into a deletion plus an addition so that each record maps to a single literal path.

Binary files report `-` instead of line counts. They are counted in `files_changed` but skipped for diff content.

### Diff filtering and budget

For each changed file, `skip_reason` checks:

- Path segments for generated/vendored directories: `node_modules`, `dist`, `build`, `.git`, `vendor`, `coverage`.
- Basenames for lockfiles: `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`, `Cargo.lock`, `go.sum`.
- Suffixes for minified assets: `.min.js`, `.min.css`.
- Extensions for binaries, images, media, archives, fonts, and compiled artifacts.
- Git's binary marker (`-` in numstat).

Remaining files are sorted by change size (added + deleted lines) ascending. This prioritizes smaller source and config files over large generated text files. Each file's patch is fetched with `git diff ... -- :(literal)<path>`, which treats the path literally rather than as a pathspec glob.

The total textual diff is capped at 15,000 characters. If a patch does not fit, it is truncated with a marker and the budget is exhausted. Files skipped by a rule, detected as binary, too large, or omitted because the budget ran out are listed in the prompt with a reason.

## Prompt

The system prompt establishes the role and SemVer 2.0.0 rules:

- Breaking/incompatible change: `major`.
- New backward-compatible functionality: `minor`.
- Backward-compatible fix, refactor, chore, docs, tests, build/tooling: `patch`.
- Mixed changes take the highest-impact bump.
- Pre-1.0 (`0.y.z`) breaking changes may map to `minor`, with a note in the reasoning.

The user prompt contains the range, commit count, file count, insertion/deletion totals, commit metadata, the filtered diff, and the skipped-file list. The model is told to return exactly:

```json
{"bump": "major|minor|patch", "reasoning": "one paragraph", "confidence": 0.0}
```

## Provider selection

`MODELS` is an ordered dict and the single source of truth for provider model IDs. Provider selection walks it in order and uses the first environment variable that is set:

1. `OPENROUTER_API_KEY`
2. `GEMINI_API_KEY`
3. `GROQ_API_KEY`

All three providers are accessed through the `openai` Python SDK with different `base_url` values, including Gemini's OpenAI-compatible endpoint. This avoids a second SDK and keeps the dependency footprint small.

`--model` overrides the selected provider's default model. The API key still comes from the selected environment variable.

Requests set `temperature=0`, a 60-second timeout, and `max_retries=0`. The only retry is the explicit one for malformed JSON, which keeps behavior predictable and avoids hidden retry storms.

`SVB_BASE_URL` can override the provider base URL. It exists mainly so the full pipeline can be tested against a local mock server; it is not required for normal use.

## Parsing and retry

`parse_analysis` accepts a JSON object possibly wrapped in a markdown code fence, finds the outermost object, and validates:

- `bump` is exactly `major`, `minor`, or `patch`.
- `reasoning` is a non-empty string.
- `confidence` is a number from 0.0 to 1.0.

On the first parse failure, `svb` sends the previous response back with a stricter "JSON only" instruction and tries again. On the second failure, it raises `ParseError`, prints the raw response to stderr, and exits with status 1.

## Version calculation

`--current` must be a three-part numeric version (`x.y.z`). The target version is computed after the model returns its recommendation:

- `major` -> `(x+1).0.0`
- `minor` -> `x.(y+1).0`
- `patch` -> `x.y.(z+1)`

The current version is validated before the model call so invalid input fails fast without spending tokens.

## Output contract

### Human-readable

The default output is:

```text
From:   <short-sha> (<subject>)
To:     <short-sha> (<subject>)
Commits: N   Files: N   +X/-Y

Recommended bump: <MAJOR|MINOR|PATCH>
Reasoning: <paragraph>
Target version: <x.y.z>        # only with --current
```

### JSON

`--json` prints exactly one JSON object to stdout:

```json
{
  "bump": "minor",
  "reasoning": "...",
  "confidence": 0.86,
  "from": "<full-sha>",
  "to": "<full-sha>",
  "commits": 4,
  "files_changed": 6,
  "target_version": "1.5.0"
}
```

`target_version` is `null` without `--current`. No logging or status text is written to stdout in JSON mode. All errors go to stderr.

## Error handling

User-facing failures raise `SvbError` and exit with status 1. This covers:

- Not inside a Git repository.
- `--from` or `--to` does not resolve to a commit.
- Empty commit range.
- Missing provider API key.
- Invalid `--current` value.
- Provider request failure or empty response.
- Unparseable model response after retry.

Errors never print a traceback in normal operation.

## Design decisions and tradeoffs

**One file, few dependencies.** The entire tool fits in one module and depends on `typer` and `openai` at runtime. This keeps the PyInstaller build simple and the code easy to audit.

**Subprocess git.** The tool uses the user's installed `git`, respecting their configuration, aliases for revisions, and repository state without adding a large Python dependency.

**Partial diffs.** The 15,000-character cap keeps prompts within free-tier context and rate limits. Filtering and size ordering reduce the chance that generated files crowd out useful source changes.

**Provider fallback by environment.** There is no config file or interactive setup. The first key found wins, which makes the tool easy to use in CI and Docker.

**No hidden retries.** The SDK retry count is zero. The only retry is the explicit JSON-format retry. Network failures surface immediately.

**No writes to the repository.** `svb` is read-only with respect to Git and project files. It never tags, commits, edits versions, or pushes.

## Security and privacy

- API keys are read from environment variables and never printed.
- The selected provider receives commit metadata and diff content. Users should avoid running `svb` on ranges containing secrets.
- Lockfiles and binaries are skipped, but any text file within the diff budget can be sent. This is inherent to the tool's purpose and is documented in the README.
- The Docker image does not bake in secrets. Compose passes keys from the environment or an ignored `.env` file.

## Non-goals

The architecture intentionally excludes:

- Release automation, file editing, tagging, committing, or pushing.
- Changelog generation.
- Config files, setup wizards, or interactive prompts.
- Multiple subcommands.
- Ollama or local model support.
- Caching, telemetry, plugins, or retry policies beyond the single JSON retry.
- A test suite beyond smoke and end-to-end verification.
