# svb - Semantic Version Bump

`svb` reads a git commit range, sends the commit metadata and diffs to a free AI model, and prints a recommended semantic version bump (`major`, `minor`, or `patch`) with a short reasoning.

It is deliberately small: one command, one job. It does **not** edit version files, create tags, commit, push, or generate changelogs.

## Features

- One command: `svb --from <commit-or-tag> [--to <commit>]`.
- Reads real commit subjects, bodies, authors, dates, diff stats, and diffs.
- Filters out lockfiles, minified files, binaries, media, and generated/vendored directories before calling the model.
- Caps the diff sent to the model at about 15,000 characters and prioritizes smaller source/config files.
- Supports three free-tier providers with automatic fallback based on environment variables.
- Human-readable output or machine-readable JSON with `--json`.
- Optionally computes the target version from `--current x.y.z`.
- Retries once when the model returns malformed JSON.
- Can be built as a single-file binary that runs without Python installed.

## Requirements

- `git` available on `PATH`.
- One API key from OpenRouter, Google Gemini, or Groq.
- Python 3.9+ only if you install from source. The compiled binary does not need Python.

## Installation

### Prebuilt binary

Download or build `dist/svb`, put it somewhere on your `PATH`, and make it executable:

```bash
chmod +x svb
sudo mv svb /usr/local/bin/svb
svb --help
```

### From source

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
svb --help
```

With `uv`:

```bash
uv venv --python python3
source .venv/bin/activate
uv pip install -e '.[dev]'
svb --help
```

Using `--python python3` prefers your system Python. Some uv-managed standalone Python builds ship a `libpython` with an executable stack, which can cause a `cannot enable executable stack` error when running a PyInstaller binary. If you hit that, build in Docker or use a venv created from your system Python.

### Docker

Docker is the recommended reproducible development environment.

```bash
docker compose build
docker compose run --rm svb svb --help
```

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the full Docker workflow, binary builds, and end-to-end testing.

## Provider setup

`svb` uses the first key it finds in this order:

| Order | Environment variable | Default model | Provider |
| --- | --- | --- | --- |
| 1 | `OPENROUTER_API_KEY` | `deepseek/deepseek-chat-v3-0324:free` | OpenRouter |
| 2 | `GEMINI_API_KEY` | `gemini-2.0-flash` | Google Gemini |
| 3 | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | Groq |

Get a key from:

- OpenRouter: <https://openrouter.ai/keys>
- Google AI Studio: <https://aistudio.google.com/app/apikey>
- Groq Console: <https://console.groq.com/keys>

Set one of them:

```bash
export OPENROUTER_API_KEY="sk-or-..."
# or
export GEMINI_API_KEY="..."
# or
export GROQ_API_KEY="gsk_..."
```

For local use, copy `.env.example` to `.env`, fill in one key, and load it:

```bash
cp .env.example .env
set -a
source .env
set +a
```

Docker Compose reads `.env` automatically for the variables listed in `compose.yaml`.

If no key is set, `svb` exits with status 1 and lists the three accepted environment variables.

## Usage

```text
svb --from <commit-or-tag> [--to <commit>] [--current <x.y.z>] [--json] [--model <name>]
```

- `--from` (required): start of the range. This commit is **exclusive**.
- `--to` (optional): end of the range. Defaults to `HEAD`. This commit is **inclusive**.
- `--current` (optional): current version in `x.y.z` form. When provided, `svb` also prints the target version.
- `--json` (optional): print only a JSON object.
- `--model` (optional): override the provider's default model.

The range is evaluated as `git log <from>..<to>`.

### Examples

Recommend a bump since the last tag:

```bash
svb --from v1.4.0
```

Recommend a bump for an explicit range:

```bash
svb --from v1.4.0 --to v1.5.0
```

Compute the next version:

```bash
svb --from v1.4.0 --current 1.4.0
```

Machine-readable output for CI:

```bash
svb --from v1.4.0 --current 1.4.0 --json
```

Override the model:

```bash
svb --from HEAD~10 --model "google/gemini-2.0-flash-001"
```

Use a specific commit instead of a tag:

```bash
svb --from 3f2a1c9 --to HEAD
```

## Output

### Human-readable

```text
From:   3f2a1c9 (Add user export endpoint)
To:     9b8c7d6 (Fix export encoding)
Commits: 4   Files: 6   +182/-37

Recommended bump: MINOR
Reasoning: The range adds a new user export endpoint and supporting tests without changing existing public behavior, so this is a backward-compatible feature.
Target version: 1.5.0
```

`Target version` is printed only when `--current` is supplied.

### JSON

```json
{"bump":"minor","reasoning":"The range adds a new user export endpoint and supporting tests without changing existing public behavior, so this is a backward-compatible feature.","confidence":0.86,"from":"3f2a1c9d4e5f60718293a4b5c6d7e8f901234567","to":"9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c","commits":4,"files_changed":6,"target_version":"1.5.0"}
```

`target_version` is `null` when `--current` is not supplied. Errors always go to stderr, so JSON consumers can rely on stdout being either one JSON object or empty.

## What is sent to the model

`svb` collects, in one logical pass:

1. Commit metadata for `<from>..<to>`: full and short SHA, author, author date, subject, and body.
2. Diff stats: files changed, insertions, deletions.
3. Textual diffs, capped at about 15,000 characters.

Before building the prompt, `svb` skips:

- Lockfiles: `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`, `Cargo.lock`, `go.sum`.
- Minified files: `*.min.js`, `*.min.css`.
- Binary and media files, based on extension and git's binary detection.
- Generated or vendored directories: `node_modules/`, `dist/`, `build/`, `.git/`, `vendor/`, `coverage/`.
- Files with very large change sets or files that do not fit in the remaining diff budget.

Skipped and omitted files are listed in the prompt so the model knows they exist. Smaller text files are prioritized over larger ones.

Commit bodies are truncated per commit, and very long ranges show the most recent 200 commits in the prompt while still reporting the true commit count.

## How the bump is chosen

The model is instructed to apply Semantic Versioning 2.0.0:

- **major**: incompatible or breaking changes to a public API, or removal of existing functionality.
- **minor**: new functionality added in a backward-compatible way.
- **patch**: backward-compatible bug fixes, refactors, performance improvements, chores, docs, tests, or build/tooling changes.
- Mixed changes take the highest-impact bump.
- Pre-1.0 (`0.y.z`) breaking changes may reasonably map to `minor`, with that judgement noted in the reasoning.

The model must return strict JSON:

```json
{"bump": "major|minor|patch", "reasoning": "one paragraph", "confidence": 0.0}
```

If parsing fails, `svb` retries once with a stricter instruction. If the second response still cannot be parsed, it exits with status 1 and prints the raw response to stderr.

## Development

Docker-first workflow:

```bash
docker compose build
docker compose run --rm svb svb --help
docker compose run --rm svb make build
```

Local workflow:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
svb --help
```

Full details, including running against another repository, building binaries with PyInstaller or Nuitka, and testing with a local mock model server, are in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

Design notes and the data flow are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Building a standalone binary

Primary path, using PyInstaller:

```bash
make build
./dist/svb --help
```

Alternative, using Nuitka:

```bash
pip install -e '.[nuitka]'
make build-nuitka
./dist/svb --help
```

Build inside Docker for a portable Linux binary:

```bash
make build-docker
# or
docker compose run --rm svb make build
```

The PyInstaller binary bundles Python and its dependencies. Build it on an older glibc-based distribution if you need it to run on older Linux systems.

If you use a uv-managed standalone Python and the resulting binary fails with `cannot enable executable stack`, rebuild with a venv created from your system Python or use the Docker build above.

## Troubleshooting

**`Error: No API key found...`**  
Set `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, or `GROQ_API_KEY`. The first one found wins.

**`Error: Not inside a git repository...`**  
Run `svb` from inside a Git working tree, or use the Docker command that mounts the target repository.

**`Error: --from '...' does not resolve to a commit.`**  
Check the tag or SHA. Fetch tags with `git fetch --tags` if needed.

**`Error: Commit range ... is empty`**  
`--from` is exclusive. If you want to include that commit, start from its parent or pass an earlier commit.

**Provider request failed**  
Check your key, network access, and whether the selected free model is still available. You can override the model with `--model`.

**Model returned malformed JSON twice**  
`svb` prints the raw response to stderr for debugging. Rerun, or try a different model with `--model`.

**Docker build fails on UID/GID**  
Set `UID` and `GID` to your host user:

```bash
UID=$(id -u) GID=$(id -g) docker compose build
```

## Limitations and non-goals

- `svb` only recommends a bump. It never edits files, tags, commits, or pushes.
- No config files, setup wizard, subcommands, caching, telemetry, or local-model support.
- No changelog generation.
- Repository data is sent to the selected AI provider. Do not run it on ranges containing secrets you are not willing to share with that provider.
- The diff budget means very large ranges are analyzed from partial content.
- The recommendation is advisory; review it before releasing.

## Project layout

```text
.
├── src/svb/__init__.py       # package metadata
├── src/svb/__main__.py       # CLI, git collection, AI call, output
├── src/svb/py.typed          # typing marker
├── pyproject.toml            # package metadata and entry point
├── uv.lock                   # reproducible dependency lockfile
├── Makefile                  # build, smoke, and Docker targets
├── Dockerfile                # development image
├── compose.yaml              # Docker Compose service
├── .env.example              # provider key template
├── .gitattributes            # cross-platform line endings and binaries
├── .gitignore
├── .dockerignore
├── README.md
└── docs/
    ├── ARCHITECTURE.md
    └── DEVELOPMENT.md
```
