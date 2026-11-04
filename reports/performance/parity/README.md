# Parity gate

`baseline/` holds the SHA-256 manifests captured from commit `bfa7613` (the tree
before any optimisation) with the unchanged production adapters:

| workload | path | bars | sections compared |
|---|---|---:|---:|
| B0 | single instrument, January | 744 | 16 |
| B1 | single instrument, January–March | 2,160 | 16 (incl. 7,995 audit events) |
| B3 | 10 instruments, January–March, with funding | 21,600 | 30 (incl. 21,600 decisions) |

Each manifest records a whole-section digest plus 64-bar chunk digests for
per-bar sections, so any divergence localises to a bar range. Volatile
run-identity fields (`recorded_time_ns`, `init_id`, `event_id`) are excluded and
their counts are recorded so the excluded set cannot grow silently.

Reproduce / extend:

    docker run --rm -v "$PWD/tools/performance:/work/tools/performance:ro" \
      -v /tape:/tape:ro -v "$PWD/reports/performance/parity:/out" v9-performance:local \
      .venv/bin/python tools/performance/parity_capture.py --tape /tape/q1 --bars 2160 \
      --workload B1 --manifest /out/candidate/B1.json
    docker run --rm ... .venv/bin/python tools/performance/parity_diff.py \
      --left /out/baseline/B1.json --right /out/candidate/B1.json

Control: re-capturing B0/B1/B3 in a fresh process against this baseline reported
PARITY on all three (16/16/30 sections), so the gate is stable across runs and
sensitive only to real content changes.
