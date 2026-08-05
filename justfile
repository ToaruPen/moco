default:
    @just --list

sync:
    uv sync --all-groups
    npm ci
    npm exec playwright install chromium webkit

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

test-browser:
    npm run test:e2e

dead-code:
    uv run vulture

dependencies:
    uv run deptry .

ast-grep:
    npm exec -- ast-grep test --skip-snapshot-tests
    npm exec -- ast-grep scan src

secret-scan:
    npm exec -- secretlint .

check: format-check lint typecheck dead-code dependencies ast-grep test-cov test-browser secret-scan build

doctor *args:
    uv run moco doctor {{args}}

run *args:
    uv run moco run {{args}}

open *args:
    uv run moco open {{args}}

install-service *args:
    uv run moco service install {{args}}

build:
    uv build
