#!/usr/bin/env python3
r"""
Mininet topology + automated experiment runner.

    client1 --\                 /-- server1 (30 Mbps, 2 ms)
               s1 -- Ryu (c0) --
    client2 --/                 \-- server2 (20 Mbps, 10 ms)

Start the controller FIRST (ryu-manager controller.py), then:
    sudo python3 topo.py                                   # interactive CLI
    sudo python3 topo.py --experiment --label dynamic      # automated tests
(restart the controller with LB_MODE=static and run with --label static
 to get the baseline, then run python3 compare.py)
"""
import argparse
import json
import os
import statistics
import threading
import time
from collections import Counter
from subprocess import PIPE, STDOUT

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
CLIENT = os.path.join(HERE, 'client_socket.py')
SERVER = os.path.join(HERE, 'server_socket.py')


def build(ctrl_ip, ctrl_port):
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink,
                  autoSetMacs=False)
    net.addController('c0', controller=RemoteController, ip=ctrl_ip,
                      port=ctrl_port)
    s1 = net.addSwitch('s1', protocols='OpenFlow13')

    c1 = net.addHost('client1', ip='10.0.0.1/24', mac='00:00:00:00:01:01')
    c2 = net.addHost('client2', ip='10.0.0.2/24', mac='00:00:00:00:01:02')
    sv1 = net.addHost('server1', ip='10.0.0.10/24', mac='00:00:00:00:00:01')
    sv2 = net.addHost('server2', ip='10.0.0.20/24', mac='00:00:00:00:00:02')

    # NOTE: keep in sync with SERVER_META in controller.py
    net.addLink(c1, s1, bw=100, delay='5ms')
    net.addLink(c2, s1, bw=100, delay='15ms')
    net.addLink(sv1, s1, bw=30, delay='2ms')
    net.addLink(sv2, s1, bw=20, delay='10ms')
    return net


def start_servers(net):
    for n in ('server1', 'server2'):
        h = net.get(n)
        h.cmd('python3 %s %s > /tmp/%s.log 2>&1 &' % (SERVER, n, n))


def run_clients(net, specs):
    procs = []
    for host, args in specs:
        p = net.get(host).popen(['python3', CLIENT] + args, stdout=PIPE,
                                stderr=STDOUT, universal_newlines=True)
        procs.append((host, p))
    out = {}
    for host, p in procs:
        text, _ = p.communicate()
        out[host] = None
        for line in text.splitlines():
            if line.startswith('SUMMARY '):
                out[host] = json.loads(line[8:])
    return out


def aggregate(results, wall):
    recs = [r for s in results.values() if s for r in s['records']]
    ok = [r for r in recs if r['ok']]
    lat = sorted(r['latency'] for r in ok)
    return {
        'requests': len(recs),
        'succeeded': len(ok),
        'failed': len(recs) - len(ok),
        'retried': sum(1 for r in recs if r['attempts'] > 1),
        'avg_latency_s': round(statistics.mean(lat), 3) if lat else None,
        'p95_latency_s': round(lat[int(0.95 * (len(lat) - 1))], 3) if lat else None,
        'avg_throughput_mbps': round(statistics.mean(r['mbps'] for r in ok), 2) if ok else None,
        'aggregate_throughput_mbps': round(sum(r['bytes'] for r in ok) * 8 / 1e6 / wall, 2),
        'wall_time_s': round(wall, 2),
        'per_server': dict(Counter(r['server'] for r in ok)),
    }


def scenario(net, name, client_args, fail_at=None):
    info('\n=== Scenario: %s ===\n' % name)
    timer = None
    if fail_at is not None:
        timer = threading.Timer(
            fail_at, lambda: net.configLinkStatus('server1', 's1', 'down'))
        timer.start()
        info('(server1 link will go DOWN after %.1fs)\n' % fail_at)
    t0 = time.time()
    res = run_clients(net, [('client1', client_args), ('client2', client_args)])
    wall = time.time() - t0
    if timer:
        timer.cancel()
        net.configLinkStatus('server1', 's1', 'up')
        time.sleep(5)                 # let heartbeat re-register server1
    agg = aggregate(res, wall)
    for k, v in agg.items():
        info('  %-26s %s\n' % (k, v))
    return agg


def experiment(net, label):
    time.sleep(4)                     # let heartbeats register the servers
    run_clients(net, [('client1', ['--count', '2', '--size', '10000'])])  # warm-up
    results = {
        'normal': scenario(net, 'normal (light load)',
                           ['--count', '6', '--parallel', '1', '--size', '1000000']),
        'heavy_load': scenario(net, 'heavy load',
                               ['--count', '8', '--parallel', '4', '--size', '2000000']),
        'server1_link_failure': scenario(
            net, 'server1 link failure during transfer',
            ['--count', '8', '--parallel', '2', '--size', '2000000'], fail_at=2.0),
    }
    fn = os.path.join(HERE, 'results_%s.json' % label)
    with open(fn, 'w') as f:
        json.dump(results, f, indent=2)
    info('\nSaved %s\n' % fn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--experiment', action='store_true')
    ap.add_argument('--label', default='dynamic',
                    help='name for results file (match controller LB_MODE)')
    ap.add_argument('--controller-ip', default='127.0.0.1')
    ap.add_argument('--controller-port', type=int, default=6653)
    args = ap.parse_args()

    setLogLevel('info')
    net = build(args.controller_ip, args.controller_port)
    net.start()
    try:
        if not net.waitConnected(timeout=15):
            info('*** Switch did not connect to the controller. '
                 'Is ryu-manager running?\n')
            return
        for h in net.hosts:
            h.cmd('sysctl -w net.ipv6.conf.all.disable_ipv6=1')
        start_servers(net)
        if args.experiment:
            experiment(net, args.label)
        else:
            info('\nServers started. Try:\n'
                 '  client1 python3 client_socket.py --count 5\n'
                 '  client2 python3 client_socket.py --count 10 --size 2000000 --parallel 4\n'
                 '  link server1 s1 down      (and "up" to restore)\n'
                 '  sh ovs-ofctl -O OpenFlow13 dump-flows s1\n'
                 '  server1 tail /tmp/server1.log\n\n')
            time.sleep(3)
            CLI(net)
    finally:
        net.stop()


if __name__ == '__main__':
    main()
