#!/bin/sh
set -eu

python -m app.infrastructure.bootstrap
exec python -m app.publisher
