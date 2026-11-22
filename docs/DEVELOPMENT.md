# Development Guide

This document covers the reproducible development workflow, how to run `svb` against repositories, how to build the standalone binary, and how to test the full pipeline without spending provider credits.

## Prerequisites

Docker-first development needs only:

- Docker Engine
- Docker Compose (the `docker compose` plugin)
- Git, for your own repository work

Local development additionally needs Python 3.9+ and `pip` or `uv`.

## Docker workflow

Build the development image:

```bash
docker compose build
```

Run the CLI:

```bash
docker compose run --rm svb svb --help
```

Open a shell in the container:

```bash
docker compose run --rm svb bash
```

The service bind-mounts the project at `/app`, so edits on the host are immediately visible. The image installs the project in editable mode and includes `git`, `make`, and PyInstaller.

### Provider keys

Compose passes these variables from your shell or from a local `.env` file:

- `OPENROUTER_API_KEY`
- `GEMINI_API_KEY`
- `GROQ_API_KEY`
- `SVB_BASE_URL` (optional, mainly for mock testing)

Start from the example file:

```bash
cp .env.example .env
```

Fill in one provider key, then run:

```bash
docker compose run --rm svb svb --from v1.0.0 --current 1.0.0
```

Do not commit `.env`; it is ignored by Git and excluded from the Docker build context.

### Run against another repository

The project directory is mounted at `/app`. To analyze a different repository, mount it separately and set the working directory:

```bash
docker compose run --rm \
  -v /path/to/target-repo:/repo \
  -w /repo \
  svb svb --from v1.0.0 --to HEAD
```

The installed `svb` executable is still available; only the working directory changes.

### Build the binary in Docker

```bash
make build-docker
# or
docker compose run --rm svb make build
```

Because the project is bind-mounted, `dist/svb` appears on the host. Building in the container gives you a Linux binary with a known glibc baseline.

### UID/GID

The compose file builds and runs as UID/GID `1000` by default so that bind-mounted build outputs are owned by your host user. If your user has different IDs:

```bash
UID=$(id -u) GID=$(id -g) docker compose build
UID=$(id -u) GID=$(id -g) docker compose run --rm svb svb --help
```

## Local workflow

Using `venv` and `pip`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
svb --help
```

Using `uv`:

```bash
uv venv --python python3
source .venv/bin/activate
uv pip install -e '.[dev]'
svb --help
```

`--python python3` prefers the system interpreter. Some uv-managed standalone Python builds ship a `libpython` with an executable stack; PyInstaller binaries built with them can fail at runtime with `cannot enable executable stack as shared object requires: Invalid argument`. In that case, use a venv created from the system Python, or build in Docker.

Run the module directly without the console script:

```bash
python -m svb --help
```

## Makefile targets

| Target | Purpose |
| --- | --- |
| `make install` | Install the project and dev tools into the active Python environment |
| `make build` | Build `./dist/svb` with PyInstaller |
| `make build-nuitka` | Build `./dist/svb` with Nuitka |
| `make build-docker` | Run `make build` inside the dev container |
| `make smoke` | Build, then run `./dist/svb --help` |
| `make clean` | Remove `build/` and `dist/` |
| `make docker-build` | Build the dev image |
| `make docker-test` | Run `svb --help` in the dev container |
| `make docker-shell` | Open a shell in the dev container |

`make build-nuitka` requires Nuitka, which is not installed by default because it is a large dependency:

```bash
pip install -e '.[nuitka]'
make build-nuitka
```

## Building the standalone binary

PyInstaller is the primary path:

```bash
make build
./dist/svb --help
```

Equivalent command:

```bash
pyinstaller --onefile --name svb \
  --distpath dist \
  --workpath build/pyinstaller \
  --specpath build \
  src/svb/__main__.py
```

Nuitka alternative:

```bash
nuitka --onefile --output-dir=dist --output-filename=svb src/svb/__main__.py
```

The result is `dist/svb`. Verify it on a machine or container without Python installed:

```bash
docker run --rm -v "$PWD/dist:/dist" debian:stable-slim /dist/svb --help
```

For maximum compatibility, build on the oldest glibc distribution you intend to support. PyInstaller bundles the Python interpreter and dependencies, but the binary still links against the host C library.

## Verification checklist

Run these before considering a change complete:

```bash
# Help text and argument names
svb --help

# Error: not a git repository
(cd /tmp && svb --from HEAD) ; echo "exit=$?"

# Error: missing provider key
(cd /path/to/repo && env -u OPENROUTER_API_KEY -u GEMINI_API_KEY -u GROQ_API_KEY svb --from HEAD~1) ; echo "exit=$?"

# Error: empty range
(cd /path/to/repo && svb --from HEAD --to HEAD) ; echo "exit=$?"

# End-to-end with a provider key
svb --from HEAD~1 --current 1.2.3
svb --from HEAD~1 --current 1.2.3 --json

# Binary smoke test
make build
./dist/svb --help
```

## End-to-end testing without a provider

You can exercise the complete pipeline, including the OpenAI-compatible HTTP call, without a real API key by running a local mock server and pointing `SVB_BASE_URL` at it.

Create `/tmp/mock_openai.py`:

```python
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        content = json.dumps(
            {
                "bump": "minor",
                "reasoning": "Adds a new feature without breaking existing behavior.",
                "confidence": 0.9,
            }
        )
        body = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": "mock",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
```

Start it:

```bash
python /tmp/mock_openai.py &
```

Create a temporary repository and run the CLI:

```bash
tmp=$(mktemp -d)
cd "$tmp"
git init -q
git config user.email "dev@example.com"
git config user.name "Dev"
printf 'print("hello")\n' > app.py
git add app.py
git commit -qm "Initial commit"
printf 'print("hello world")\n' >> app.py
git commit -qam "Add world"

OPENROUTER_API_KEY=dummy \
SVB_BASE_URL=http://127.0.0.1:8080/v1 \
svb --from HEAD~1 --current 1.2.3 --json
```

Expected output is a JSON object with `"bump": "minor"` and `"target_version": "1.3.0"`.

The mock can also return malformed JSON to verify the retry path. Change the `content` value to a non-JSON string; `svb` should call the endpoint twice and then exit 1 with the raw response on stderr.

To test the compiled binary against the mock, build it and run the same command with `./dist/svb` instead of `svb`.

## Repository layout and code map

- `src/svb/__main__.py` contains the CLI, git collection, prompt construction, provider selection, model call, parsing, and output formatting.
- `src/svb/__init__.py` contains package metadata.
- `pyproject.toml` defines dependencies and the `svb` console script.
- `Dockerfile` and `compose.yaml` define the development environment.
- `Makefile` wraps common build and smoke-test commands.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the data flow and design decisions.

## Troubleshooting

**Container cannot write `dist/` or `build/`**  
Use matching UID/GID as described above.

**`pyinstaller: command not found`**  
Install dev extras (`pip install -e '.[dev]'`) or run the build in Docker.

**The binary runs locally but not on a target machine**  
Build it in Docker or on an older glibc distribution. PyInstaller does not make the binary independent of the system C library.

**Provider calls fail behind a proxy**  
Set the standard `HTTP_PROXY` / `HTTPS_PROXY` environment variables for the process or container.

**Model output is malformed twice**  
Use a different model with `--model`, or rerun. The raw response is printed to stderr for debugging.
