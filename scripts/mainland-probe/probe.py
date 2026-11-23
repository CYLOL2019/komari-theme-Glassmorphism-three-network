#!/usr/bin/env python3
"""Lightweight Mainland China TCP + ICMP reachability probe for Komari Glassmorphism.

Designed to run on any Mainland China host (directly or inside Docker with
`--network host --cap-add NET_RAW`). Supports binding directly to a physical
network interface (`--interface eth0` / `PROBE_INTERFACE`) via `SO_BINDTODEVICE`
so hosts running a local TUN proxy (Clash / mihomo / sing-box) can still probe
true direct Mainland China reachability without bypassing proxy rules globally.

Configuration (via CLI flags or environment variables — no hardcoded targets):
  PROBE_TARGETS   JSON array of targets, e.g.:
                  '[{"name":"Node-A","ip":"203.0.113.10","port":443}]'
  PROBE_INTERFACE Optional physical NIC name to bind via SO_BINDTODEVICE (Linux)
  PROBE_LOCATION  Display label for probe origin (default: "China Mainland")
  PROBE_INTERVAL  Probe interval in seconds (default: 60)
  PROBE_TIMEOUT   Per-probe timeout in seconds (default: 3)
  PROBE_LISTEN    HTTP listen address (default: "0.0.0.0:25775")
  PROBE_OUT_FILE  Optional static JSON output path (e.g. /data/mainland-status.json)
"""
from __future__ import annotations

import argparse
import datetime
import http.server
import json
import os
import re
import socket
import subprocess
import threading
import time
from typing import Any

SO_BINDTODEVICE = 25


def probe_tcp(ip: str, port: int, timeout: float, interface: str | None) -> tuple[bool, float | None]:
    family = socket.AF_INET6 if ':' in ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if interface:
            sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, (interface + '\0').encode('utf-8'))
        start = time.monotonic()
        sock.connect((ip, port))
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        return True, elapsed_ms
    except OSError:
        return False, None
    finally:
        sock.close()


def probe_icmp(ip: str, timeout: float, interface: str | None) -> tuple[bool, float | None]:
    cmd = ['ping', '-c', '2', '-W', str(max(1, int(round(timeout))))]
    if interface:
        cmd.extend(['-I', interface])
    cmd.append(ip)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout * 3 + 2)
        if proc.returncode != 0:
            return False, None
        m = re.search(r'=\s*[\d.]+/([\d.]+)/', proc.stdout)
        if m:
            return True, round(float(m.group(1)), 1)
        m2 = re.search(r'time[=<]\s*([\d.]+)\s*ms', proc.stdout)
        return True, round(float(m2.group(1)), 1) if m2 else None
    except Exception:
        return False, None


def classify(tcp_ok: bool, icmp_ok: bool) -> str:
    if tcp_ok:
        return 'ok'
    if icmp_ok:
        return 'tcp_blocked'
    return 'blocked'


def main() -> None:
    parser = argparse.ArgumentParser(description='Mainland China TCP/ICMP reachability probe')
    parser.add_argument('--targets', default=os.environ.get('PROBE_TARGETS', '[]'))
    parser.add_argument('--interface', default=os.environ.get('PROBE_INTERFACE', ''))
    parser.add_argument('--location', default=os.environ.get('PROBE_LOCATION', 'China Mainland'))
    parser.add_argument('--interval', type=int, default=int(os.environ.get('PROBE_INTERVAL', '60')))
    parser.add_argument('--timeout', type=float, default=float(os.environ.get('PROBE_TIMEOUT', '3')))
    parser.add_argument('--listen', default=os.environ.get('PROBE_LISTEN', '0.0.0.0:25775'))
    parser.add_argument('--out-file', default=os.environ.get('PROBE_OUT_FILE', ''))
    args = parser.parse_args()

    targets: list[dict[str, Any]] = json.loads(args.targets)
    interface = args.interface.strip() or None
    state_lock = threading.Lock()
    state: dict[str, Any] = {
        'updated_at': '',
        'probe_location': args.location,
        'nodes': {},
    }

    def run_once() -> None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        nodes: dict[str, Any] = {}
        for item in targets:
            key = str(item.get('uuid') or item.get('name') or '').strip()
            ip = str(item.get('ip') or '').strip()
            port = int(item.get('port') or 443)
            if not key or not ip:
                continue
            tcp_ok, tcp_ms = probe_tcp(ip, port, args.timeout, interface)
            icmp_ok, icmp_ms = probe_icmp(ip, args.timeout, interface)
            entry = {
                'icmp': icmp_ok,
                'tcp': tcp_ok,
                'port': port,
                'icmp_ms': icmp_ms,
                'tcp_ms': tcp_ms,
                'status': classify(tcp_ok, icmp_ok),
                'checked_at': now,
                'probe_location': args.location,
            }
            nodes[key] = entry
            if item.get('name') and str(item['name']).strip() != key:
                nodes[str(item['name']).strip()] = entry

        payload = {
            'updated_at': now,
            'probe_location': args.location,
            'nodes': nodes,
        }
        with state_lock:
            state.clear()
            state.update(payload)

        if args.out_file:
            tmp_path = args.out_file + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, args.out_file)

    def loop() -> None:
        while True:
            try:
                run_once()
            except Exception as exc:
                print(f'[mainland-probe] probe cycle error: {exc}', flush=True)
            time.sleep(max(15, args.interval))

    run_once()
    threading.Thread(target=loop, daemon=True).start()

    host, port_str = args.listen.rsplit(':', 1)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split('?', 1)[0] not in ('/', '/status.json', '/mainland-status.json'):
                self.send_response(404)
                self.end_headers()
                return
            with state_lock:
                body = json.dumps(state, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *msg_args: Any) -> None:
            return

    server = http.server.ThreadingHTTPServer((host, int(port_str)), Handler)
    print(f'[mainland-probe] serving on http://{args.listen}/mainland-status.json (iface={interface or "default"})', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
