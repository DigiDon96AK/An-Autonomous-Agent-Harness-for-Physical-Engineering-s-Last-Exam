#!/usr/bin/env bash
# Copyright 2026 Google LLC
# Install dependencies for the Gemini Engineering Agent Harness

set -euo pipefail

VENV_PATH="/opt/harness/venv"

echo "Setting up Python virtual environment at ${VENV_PATH}..."
uv venv --python 3.12 "${VENV_PATH}"

echo "Installing harness dependencies from requirements.txt..."
uv pip install --python "${VENV_PATH}/bin/python" -r "$(dirname "$0")/requirements.txt"

echo "Installation complete."

