# Lint runner

Dockerized linter image. CI job `lint` (`.github/workflows/tests.yml`) builds
this image and runs every linter inside it; local development uses the same
image via the `linters` compose service. There is no parallel native install —
this Dockerfile is the single source of truth for linter versions.

## Build

```bash
cd backend
docker compose --profile lint build linters
```

## Usage

All commands run from `backend/`. Source is mounted read-only at `/repo`.

```bash
# ruff
docker compose --profile lint run --rm linters \
    ruff check backend/services backend/shared

# sqlfluff
docker compose --profile lint run --rm linters \
    sqlfluff lint backend/services --dialect postgres

# bandit
docker compose --profile lint run --rm linters \
    bandit -r backend/services backend/shared -c pyproject.toml

# mypy — per-service (strict mode requires per-service cwd)
docker compose --profile lint run --rm \
    --workdir /repo/backend/services/<svc> linters \
    mypy --cache-dir=/tmp/.mypy_cache \
         --config-file /repo/pyproject.toml app

# mypy — shared
docker compose --profile lint run --rm \
    --workdir /repo/backend linters \
    mypy --cache-dir=/tmp/.mypy_cache \
         --config-file /repo/pyproject.toml shared

# hadolint
docker compose --profile lint run --rm linters sh -c \
    "hadolint backend/services/*/Dockerfile backend/gateway/Dockerfile backend/tests/gateway/Dockerfile backend/Dockerfile.backup"

# shellcheck
docker compose --profile lint run --rm linters \
    shellcheck backend/scripts/*.sh

# luacheck
docker compose --profile lint run --rm linters \
    luacheck backend/gateway/lua/

# auto-fix (requires writable mount; overrides the default read-only volume)
docker compose --profile lint run --rm \
    --volume "$(pwd)/..:/repo" linters \
    ruff check backend/services backend/shared --select <RULE> --fix
```

### Git Bash / MSYS path conversion

On Git Bash for Windows, paths starting with `/` get auto-translated. Prefix
with `MSYS_NO_PATHCONV=1` or escape with double slashes (`//repo/...`).

## Tools and versions

`MYPYPATH=/repo/backend` is baked into the compose service so `shared.*`
imports resolve. Bump versions in this Dockerfile only — CI builds from it.

| Tool | Version | Source |
|---|---|---|
| ruff | 0.7.4 | pip |
| mypy | 1.13.0 | pip |
| bandit | 1.7.10 | pip |
| sqlfluff | 3.2.5 | pip |
| hadolint | 2.12.0 | curl release binary |
| shellcheck | apt (Debian Trixie) | apt |
| luacheck | latest | luarocks |

Runtime deps installed alongside the linters (so mypy resolves third-party
imports): tornado 6.4.1, dacite 1.8.1, psycopg2-binary 2.9.10, bcrypt 4.2.0,
cryptography 43.0.1, requests 2.32.3, types-requests 2.32.0.20240914.

## Base image

`python:3.11-slim` (Debian Trixie), apt sources switched to
`https://mirror.yandex.ru/debian`. NOT subject to Astra certification — lint
tooling never enters production builds.
