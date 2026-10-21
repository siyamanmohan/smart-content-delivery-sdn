#!/usr/bin/env python3
"""Print static (baseline) vs dynamic results side by side."""
import json

with open('results_static.json') as f:
    st = json.load(f)
with open('results_dynamic.json') as f:
    dy = json.load(f)

metrics = ['succeeded', 'failed', 'retried', 'avg_latency_s', 'p95_latency_s',
           'avg_throughput_mbps', 'aggregate_throughput_mbps', 'wall_time_s',
           'per_server']
for sc in dy:
    print('\n== %s ==' % sc)
    print('%-28s %-26s %-26s' % ('metric', 'static (baseline)', 'dynamic (SDN)'))
    for m in metrics:
        print('%-28s %-26s %-26s' % (m, st[sc].get(m), dy[sc].get(m)))
