#!/usr/bin/env sh
# Start the Conversational BI server (builds the demo database on first run).
cd "$(dirname "$0")" && exec python3 -m bi.server
