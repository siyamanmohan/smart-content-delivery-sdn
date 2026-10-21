#!/usr/bin/env python3
"""
Content server (TCP, multi-threaded).

Usage:  python3 server_socket.py <name> [--port 8080]

Protocol (HTTP-like, text header + binary body):
    request :  GET /content?size=<bytes> HTTP/1.1\r\n ... \r\n\r\n
    response:  HTTP/1.1 200 OK\r\n Content-Length: N\r\n X-Server: <name>\r\n\r\n <N bytes>
A request without ?size= returns a small HTML page naming the server.

Besides serving clients, every server broadcasts a UDP heartbeat
({"name":..,"active":..}) to 10.0.0.255:9999 once per second. The SDN
controller receives it as a packet-in and uses it for discovery/availability.
"""
import argparse
import json
import re
import socket
import threading
import time

HB_ADDR = ('10.0.0.255', 9999)
MAX_SIZE = 100 * 1024 * 1024

active = 0
lock = threading.Lock()


def log(name, msg):
    print('[%s %s] %s' % (name, time.strftime('%H:%M:%S'), msg), flush=True)


def heartbeat(name, interval=1.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while True:
        with lock:
            n = active
        payload = json.dumps({'name': name, 'active': n,
                              'ts': time.time()}).encode()
        try:
            sock.sendto(payload, HB_ADDR)
        except OSError:
            pass                      # link down / network unreachable
        time.sleep(interval)


def handle(conn, addr, name):
    global active
    with lock:
        active += 1
    try:
        conn.settimeout(10)
        data = b''
        while b'\r\n\r\n' not in data and len(data) < 8192:
            chunk = conn.recv(1024)
            if not chunk:
                return
            data += chunk
        request_line = data.split(b'\r\n')[0].decode(errors='replace')
        m = re.search(r'size=(\d+)', request_line)
        if m:
            size = min(int(m.group(1)), MAX_SIZE)
            body = None
        else:
            body = ('<html><body><h1>Smart Content Delivered by %s</h1>'
                    '</body></html>' % name).encode()
            size = len(body)
        header = ('HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream'
                  '\r\nContent-Length: %d\r\nX-Server: %s\r\n'
                  'Connection: close\r\n\r\n' % (size, name)).encode()
        conn.sendall(header)
        if body is not None:
            conn.sendall(body)
        else:
            block = b'x' * 65536
            sent = 0
            while sent < size:
                n = min(len(block), size - sent)
                conn.sendall(block[:n])
                sent += n
        log(name, 'served %s %d bytes to %s:%d' % (request_line[:40], size,
                                                   addr[0], addr[1]))
    except OSError as e:
        log(name, 'connection error with %s: %s' % (addr[0], e))
    finally:
        conn.close()
        with lock:
            active -= 1


def run_server(name, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', port))
    srv.listen(128)
    log(name, 'content server listening on port %d' % port)
    threading.Thread(target=heartbeat, args=(name,), daemon=True).start()
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, addr, name),
                         daemon=True).start()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('name', nargs='?', default='server1')
    ap.add_argument('--port', type=int, default=8080)
    args = ap.parse_args()
    run_server(args.name, args.port)
