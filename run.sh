#!/usr/bin/env sh
# Run from the repository so semantic models and web assets remain local.
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python -m bi.server
fi
exec python3 -m bi.server
