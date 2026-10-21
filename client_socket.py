#!/usr/bin/env python3
"""
Content client (TCP). Talks to the controller's virtual IP.

Examples:
  python3 client_socket.py                       # 1 request, 100 KB
  python3 client_socket.py --count 10 --size 2000000 --parallel 4
Last output line is "SUMMARY {json}" (used by topo.py --experiment).
"""
import argparse
import json
import socket
import threading
import time

VIP = '10.0.0.100'


def fetch(vip, port, size, timeout, retries):
    start = time.time()
    attempts = 0
    err = ''
    while True:
        attempts += 1
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((vip, port))
            sock.sendall(('GET /content?size=%d HTTP/1.1\r\nHost: %s\r\n'
                          'Connection: close\r\n\r\n' % (size, vip)).encode())
            buf = b''
            while b'\r\n\r\n' not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError('closed before header')
                buf += chunk
            head, _, body = buf.partition(b'\r\n\r\n')
            headers = {}
            for line in head.decode(errors='replace').split('\r\n')[1:]:
                if ':' in line:
                    k, v = line.split(':', 1)
                    headers[k.strip().lower()] = v.strip()
            length = int(headers['content-length'])
            got = len(body)
            while got < length:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError('closed after %d/%d bytes'
                                          % (got, length))
                got += len(chunk)
            elapsed = time.time() - start
            return {'ok': True, 'latency': elapsed, 'bytes': got,
                    'mbps': got * 8 / 1e6 / elapsed if elapsed else 0.0,
                    'server': headers.get('x-server', '?'),
                    'attempts': attempts, 'error': ''}
        except (OSError, ValueError, KeyError) as e:
            err = '%s: %s' % (type(e).__name__, e)
        finally:
            if sock:
                sock.close()
        if attempts > retries:
            return {'ok': False, 'latency': time.time() - start, 'bytes': 0,
                    'mbps': 0.0, 'server': None, 'attempts': attempts,
                    'error': err}
        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--vip', default=VIP)
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--size', type=int, default=100000, help='bytes per request')
    ap.add_argument('--count', type=int, default=1, help='total requests')
    ap.add_argument('--parallel', type=int, default=1, help='concurrent workers')
    ap.add_argument('--timeout', type=float, default=2.0)
    ap.add_argument('--retries', type=int, default=2, help='extra attempts')
    args = ap.parse_args()

    records, lock = [], threading.Lock()
    counter = {'next': 0}

    def worker():
        while True:
            with lock:
                i = counter['next']
                if i >= args.count:
                    return
                counter['next'] += 1
            r = fetch(args.vip, args.port, args.size, args.timeout,
                      args.retries)
            with lock:
                records.append(r)
            if r['ok']:
                print('req %-3d OK   server=%-8s %8d B  %.2fs  %.1f Mbps  '
                      'attempts=%d' % (i + 1, r['server'], r['bytes'],
                                       r['latency'], r['mbps'], r['attempts']),
                      flush=True)
            else:
                print('req %-3d FAIL after %d attempts (%s)'
                      % (i + 1, r['attempts'], r['error']), flush=True)

    t0 = time.time()
    threads = [threading.Thread(target=worker) for _ in range(args.parallel)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    print('SUMMARY ' + json.dumps({'wall': wall, 'records': records}))


if __name__ == '__main__':
    main()
