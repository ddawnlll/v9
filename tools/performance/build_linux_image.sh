#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
tar -cf - pyproject.toml uv.lock README.md src tools/performance | docker build -t v9-performance:local -f tools/performance/Dockerfile -
