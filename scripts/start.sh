#!/bin/sh
set -eu

python -m app.infrastructure.bootstrap
exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8003
