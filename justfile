default:
    @just --list

sync:
    uv sync --all-groups
    npm ci

format:
    uv run ruff format .
    uv run ruff check --fix .
    npm run format:frontend

format-check:
    uv run ruff format --check .

lint:
    uv run ruff check .
    npm run check:frontend

typecheck:
    uv run mypy

test:
    uv run pytest --durations=10
    npm run test:frontend

test-integration:
    uv run pytest -m integration --durations=10

test-live:
    uv run pytest -m live --durations=10

test-cov:
    uv run pytest -m "not live and not slow" --cov --cov-report=term-missing --cov-report=xml
    npm run test:frontend

dead-code:
    uv run vulture

dependencies:
    uv run deptry .

secret-scan:
    npm exec -- secretlint .

check: format-check lint typecheck dead-code dependencies test-cov secret-scan

doctor *args:
    uv run moco doctor {{args}}

serve *args:
    uv run moco serve {{args}}

install-service *args:
    uv run moco service install {{args}}

build:
    uv build
