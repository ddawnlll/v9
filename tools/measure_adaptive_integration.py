import json
import resource
import time
from collections import Counter
from pathlib import Path

from v8_next.adapters.adaptive_strategy import SimArmConfig
from v8_next.evaluation.sim_arm import run_baseline_arm, run_sim_arm

config = SimArmConfig(enabled=True)
tape = Path('research/tape/btcusdt-1h-2025Q1')
for name, runner in [('baseline', run_baseline_arm), ('adaptive', run_sim_arm)]:
    start_wall, start_cpu = time.perf_counter(), time.process_time()
    result = runner(tape=tape, config=config)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    print(json.dumps({'arm':name, 'wall_s':round(time.perf_counter()-start_wall, 3), 'cpu_s':round(time.process_time()-start_cpu, 3), 'process_peak_rss_bytes':usage.ru_maxrss, 'bars':result.bars, 'trades':result.trades, 'open':result.open_campaigns, 'balance_delta':result.net_pnl, 'net_r':result.net_r, 'fees':result.fee_total, 'funding':result.funding_total, 'tape_sha256':result.provenance.tape_sha256, 'decisions_digest':result.decisions_digest, 'orders_digest':result.orders_digest, 'protection_state':result.protection.get('state'), 'violations':dict(Counter(v['code'] for v in result.protection.get('violations', []))), 'unsupported':result.unsupported}), flush=True)
