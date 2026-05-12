#!/bin/bash
cd "$(dirname "$0")"

source .env 2>/dev/null || true

PYTHONPATH="/tmp/selfheal-deps:$(pwd)" python3 -m scripts.webhook_listener --host 0.0.0.0 --port 8080
