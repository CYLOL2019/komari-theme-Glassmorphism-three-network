# Mainland China Reachability Probe (`mainland-probe`)

Lightweight zero-dependency Python 3 probe service for **Komari Glassmorphism**'s China Mainland reachability (GFW / TCP block) badge.

## Why a Mainland-origin probe?

Komari's built-in three-network Ping tasks run **from the overseas VPS toward China** (typically ICMP). When an IP's inbound TCP port (e.g. `443`) is blocked from Mainland China while outbound ICMP still passes, an outbound-only ping will still appear green.

Running this tiny probe on a Mainland China host checks **both TCP (e.g. `:443`) and ICMP** directly from Mainland China toward your target servers and exposes a clean JSON feed with no private IPs or secrets in the theme code.

## Usage (Direct / systemd — Recommended)

`probe.py` uses only the Python 3 standard library (~12–18 MB RSS, zero container overhead). Direct deployment with `systemd` is the default recommended method on Linux hosts:

```bash
sudo install -d -m 755 /opt/komari-mainland-probe
sudo install -m 755 probe.py /opt/komari-mainland-probe/probe.py
sudo install -m 640 mainland-probe.env.example /etc/komari-mainland-probe.env
# Edit /etc/komari-mainland-probe.env with your physical NIC and target nodes
sudo install -m 644 komari-mainland-probe.service /etc/systemd/system/komari-mainland-probe.service
sudo systemctl daemon-reload
sudo systemctl enable --now komari-mainland-probe
```

Quick foreground test (requires `CAP_NET_RAW` only when `--interface` is set):

```bash
PROBE_LISTEN="127.0.0.1:25775" \
PROBE_TARGETS='[{"name":"US-Node-1","ip":"203.0.113.10","port":443}]' \
python3 probe.py
```

### Bypassing a local TUN proxy (Clash / mihomo / sing-box)

Set `PROBE_INTERFACE` to the host's physical NIC (for example `enp4s0` or `eth0`). The provided `komari-mainland-probe.service` grants `AmbientCapabilities=CAP_NET_RAW` (or `--network host --cap-add NET_RAW` in Docker) so sockets bind directly via `SO_BINDTODEVICE` and `ping -I <nic>`, probing real Mainland China direct routing without touching the host's TUN rules.

## Usage (Docker — Optional)

If you already manage your Komari stack with Docker Compose and prefer containerized isolation:

```bash
docker run -d \
  --name komari-mainland-probe \
  --restart unless-stopped \
  --network host \
  --cap-add NET_RAW \
  -e PROBE_LOCATION="China Mainland" \
  -e PROBE_INTERFACE="eth0" \
  -e PROBE_LISTEN="127.0.0.1:25775" \
  -e PROBE_INTERVAL="60" \
  -e PROBE_TARGETS='[
    {"name":"US-Node-1","ip":"203.0.113.10","port":443},
    {"name":"US-Node-2","ip":"198.51.100.20","port":443}
  ]' \
  -v /path/to/probe.py:/app/probe.py:ro \
  python:3.12-alpine python3 /app/probe.py
```

## Connecting to Komari Theme

1. Expose `/mainland-status.json` via your reverse proxy (same origin recommended, e.g. `location = /mainland-status.json { proxy_pass http://127.0.0.1:25775/mainland-status.json; }`) or directly with CORS enabled.
2. In **Komari Admin → Theme Settings → 中国大陆连通性检测**, set **外部探针 JSON 地址** to `/mainland-status.json` (or your full URL).
3. Matching works by node `uuid` or node `name` (case-insensitive).

### JSON Output Schema

```json
{
  "updated_at": "2026-01-01T00:00:00Z",
  "probe_location": "China Mainland",
  "nodes": {
    "US-Node-1": {
      "icmp": false,
      "tcp": false,
      "port": 443,
      "icmp_ms": null,
      "tcp_ms": null,
      "status": "blocked",
      "checked_at": "2026-01-01T00:00:00Z",
      "probe_location": "China Mainland"
    }
  }
}
```

Status values:

- `ok`: TCP reachable (`大陆直连`)
- `tcp_blocked`: TCP blocked/timeout while ICMP reachable (`TCP阻断`)
- `blocked`: Both TCP and ICMP unreachable (`疑似被墙`)
