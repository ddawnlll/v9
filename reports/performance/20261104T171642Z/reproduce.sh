#!/bin/sh
set -eu
uv sync --locked --extra dev
for bars in 720 1440 2880; do
  for run in 1 2 3; do
    uv run --locked --extra dev python tools/performance/ensemble_profile.py --bars "$bars" --output "/tmp/v9-${bars}-${run}.json"
  done
done
uv run --locked --extra dev python tools/performance/ensemble_profile.py --bars 2880 --profile /tmp/v9-profile.pstats --output /tmp/v9-profile.json
uv run --locked --extra dev python -c 'import pstats; pstats.Stats("/tmp/v9-profile.pstats").sort_stats("cumulative").print_stats(60)'
