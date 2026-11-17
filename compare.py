#!/usr/bin/env python3
"""Print static (baseline) vs dynamic results side by side."""
from pathlib import Path
import json

HERE = Path(__file__).resolve().parent

with (HERE / 'results_static.json').open() as f:
    st = json.load(f)
with (HERE / 'results_dynamic.json').open() as f:
    dy = json.load(f)

metrics = ['succeeded', 'failed', 'retried', 'avg_latency_s', 'p95_latency_s',
           'avg_throughput_mbps', 'aggregate_throughput_mbps', 'wall_time_s',
           'per_server']
for sc in dy:
    print('\n== %s ==' % sc)
    print('%-28s %-26s %-26s' % ('metric', 'static (baseline)', 'dynamic (SDN)'))
    for m in metrics:
        print('%-28s %-26s %-26s' % (m, st[sc].get(m), dy[sc].get(m)))
