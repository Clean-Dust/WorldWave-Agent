#!/bin/bash
# Start WW in test mode — reads .env for all config
cd "$(dirname "$0")"
set -a
source .env
set +a
exec python3 server.py "$@"
