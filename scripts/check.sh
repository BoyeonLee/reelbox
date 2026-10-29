#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f pyproject.toml ] || { echo "아직 프로젝트 없음 - 검사 건너뜀"; exit 0; }
uv run ruff check .
uv run ruff format --check .
uv run pytest -q