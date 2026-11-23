#!/usr/bin/env python3
"""Declarative YAML/JSON export & import tool for Komari Monitor.

Export a Komari server's nodes (billing, traffic quotas, tags, groups, notes),
ping tasks (referenced by node name instead of fragile UUIDs), site settings,
and active theme settings into a portable YAML or JSON file — then re-apply it
idempotently to a new or existing Komari instance in one command.

Usage:
  python3 komari_sync.py export --url http://127.0.0.1:25774 -u admin -p '***' -o komari-config.yml
  python3 komari_sync.py diff   --url http://127.0.0.1:25774 -u admin -p '***' -f komari-config.yml
  python3 komari_sync.py apply  --url http://127.0.0.1:25774 -u admin -p '***' -f komari-config.yml
  python3 komari_sync.py apply  --url https://status.example.com -f komari-config.json --emit-agents https://status.example.com

Environment variables (avoid putting passwords in shell history):
  KOMARI_URL, KOMARI_USERNAME, KOMARI_PASSWORD
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None


SAFE_SITE_KEYS = (
    "sitename",
    "description",
    "custom_head",
    "custom_body",
    "geo_ip_enabled",
    "geo_ip_provider",
    "expire_notification_enabled",
    "expire_notification_lead_days",
    "ping_record_preserve_time",
    "record_enabled",
    "record_preserve_time",
    "private_site",
    "cors_origin_check_enabled",
    "cors_allowed_origins",
)

EDITABLE_NODE_KEYS = (
    "name",
    "region",
    "group",
    "tags",
    "weight",
    "price",
    "billing_cycle",
    "auto_renewal",
    "currency",
    "expired_at",
    "traffic_limit",
    "traffic_limit_type",
    "remark",
    "public_remark",
    "hidden",
)

INTERNAL_THEME_KEYS = {"_komari_onboarding_v1"}

BYTE_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def parse_byte_size(value: Any) -> int:
    """Parse integer bytes or human strings such as '200 GiB', '2 TiB', '500 GB'."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip()
    if not text:
        return 0
    if re.fullmatch(r"\d+", text):
        return int(text)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-z]+)", text)
    if not m:
        raise ValueError(f"Invalid byte size: {value!r}")
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit not in BYTE_UNITS:
        raise ValueError(f"Unknown byte unit {m.group(2)!r} in {value!r}")
    return int(round(num * BYTE_UNITS[unit]))


def format_byte_size(num: int | None) -> int | str:
    """Format exact binary/decimal multiples cleanly while preserving exact bytes."""
    if not num:
        return 0
    for suffix, factor in (
        ("TiB", 1024**4),
        ("GiB", 1024**3),
        ("MiB", 1024**2),
        ("TB", 1000**4),
        ("GB", 1000**3),
        ("MB", 1000**2),
    ):
        if num % factor == 0:
            return f"{num // factor} {suffix}"
    return int(num)


def normalize_expired_at(value: Any) -> str | None:
    """Normalize Komari timestamp strings to ISO-8601 UTC or None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("0001-01-01"):
        return None
    text = text.replace(" ", "T")
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text + "T00:00:00Z"
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.year <= 1:
            return None
        utc = parsed.astimezone(dt.timezone.utc).replace(microsecond=0)
        return utc.isoformat().replace("+00:00", "Z")
    except ValueError:
        return text


def detect_format(path: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit.lower()
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        return "json"
    if ext in (".yml", ".yaml"):
        return "yaml"
    return "yaml" if yaml is not None else "json"


def load_config_file(path: str, fmt: str | None = None) -> dict[str, Any]:
    resolved = detect_format(path, fmt)
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    if resolved == "json":
        data = json.loads(raw)
    else:
        if yaml is None:
            raise RuntimeError(
                "PyYAML is required to read YAML files (pip install pyyaml), or use a .json file."
            )
        data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Expected top-level mapping in {path}")
    return data


def dump_config_file(data: dict[str, Any], path: str, fmt: str | None = None) -> None:
    resolved = detect_format(path, fmt)
    if resolved == "json":
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    else:
        if yaml is None:
            raise RuntimeError(
                "PyYAML is required to write YAML files (pip install pyyaml), or export to a .json file."
            )
        header = (
            "# Komari declarative server configuration exported by komari_sync.py\n"
            "# Re-apply to any Komari instance with: python3 komari_sync.py apply -f <this-file>\n"
        )
        text = header + yaml.safe_dump(
            data,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=120,
        )
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class KomariClient:
    def __init__(self, base_url: str, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )

    def request_json(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
    ) -> Any:
        url = self.base_url + path
        headers = {
            "Accept": "application/json",
            "Origin": self.origin,
            "Referer": self.origin + "/admin",
        }
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {body[:400]}") from exc
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{method} {path} returned non-JSON response: {raw[:200]}"
            ) from exc
        if isinstance(parsed, dict) and parsed.get("status") == "error":
            raise RuntimeError(
                f"{method} {path} failed: {parsed.get('message') or parsed}"
            )
        return parsed

    def login(self, username: str, password: str) -> None:
        self.request_json(
            "POST",
            "/api/login",
            {"username": username, "password": password},
        )

    def get_public(self) -> dict[str, Any]:
        res = self.request_json("GET", "/api/public")
        return res.get("data", res) if isinstance(res, dict) else {}

    def get_settings(self) -> dict[str, Any]:
        res = self.request_json("GET", "/api/admin/settings")
        return res.get("data", res) if isinstance(res, dict) else {}

    def update_settings(self, updates: dict[str, Any]) -> Any:
        return self.request_json("POST", "/api/admin/settings", updates)

    def list_clients(self) -> list[dict[str, Any]]:
        res = self.request_json("GET", "/api/admin/client/list")
        if isinstance(res, list):
            return res
        if isinstance(res, dict) and isinstance(res.get("data"), list):
            return res["data"]
        raise RuntimeError(f"Unexpected /api/admin/client/list payload: {res!r}")

    def add_client(self, name: str) -> dict[str, Any]:
        res = self.request_json("POST", "/api/admin/client/add", {"name": name})
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            return res["data"]
        if isinstance(res, dict):
            return res
        raise RuntimeError(f"Unexpected /api/admin/client/add payload: {res!r}")

    def edit_client(self, uuid: str, fields: dict[str, Any]) -> Any:
        payload = {"uuid": uuid, **fields}
        return self.request_json(
            "POST", f"/api/admin/client/{urllib.parse.quote(uuid, safe='')}/edit", payload
        )

    def list_ping_tasks(self) -> list[dict[str, Any]]:
        res = self.request_json("GET", "/api/admin/ping")
        if isinstance(res, list):
            return res
        if isinstance(res, dict) and isinstance(res.get("data"), list):
            return res["data"]
        return []

    def add_ping_task(self, task: dict[str, Any]) -> Any:
        return self.request_json("POST", "/api/admin/ping/add", task)

    def edit_ping_tasks(self, tasks: list[dict[str, Any]]) -> Any:
        return self.request_json("POST", "/api/admin/ping/edit", {"tasks": tasks})

    def delete_ping_tasks(self, ids: list[int]) -> Any:
        return self.request_json("POST", "/api/admin/ping/delete", {"id": ids})

    def list_themes(self) -> list[dict[str, Any]]:
        res = self.request_json("GET", "/api/admin/theme/list")
        if isinstance(res, dict) and isinstance(res.get("data"), list):
            return res["data"]
        return []

    def set_theme(self, short: str) -> Any:
        q = urllib.parse.urlencode({"theme": short})
        return self.request_json("GET", f"/api/admin/theme/set?{q}")

    def update_theme_settings(self, short: str, settings: dict[str, Any]) -> Any:
        q = urllib.parse.urlencode({"theme": short})
        return self.request_json("POST", f"/api/admin/theme/settings?{q}", settings)


def canonicalize_node(node: dict[str, Any]) -> dict[str, Any]:
    """Normalize a node record (live or desired) for clean diffing & export."""
    out: dict[str, Any] = {
        "name": str(node.get("name") or "").strip(),
        "region": str(node.get("region") or "").strip(),
        "group": str(node.get("group") or "").strip(),
        "tags": str(node.get("tags") or "").strip(),
        "weight": int(node.get("weight") or 0),
        "price": float(node.get("price") or 0),
        "billing_cycle": int(node.get("billing_cycle") or 0),
        "auto_renewal": bool(node.get("auto_renewal", False)),
        "currency": str(node.get("currency") or "$"),
        "expired_at": normalize_expired_at(node.get("expired_at")),
        "traffic_limit": parse_byte_size(node.get("traffic_limit")),
        "traffic_limit_type": str(node.get("traffic_limit_type") or "max"),
        "remark": str(node.get("remark") or ""),
        "public_remark": str(node.get("public_remark") or ""),
        "hidden": bool(node.get("hidden", False)),
    }
    if out["price"].is_integer():
        out["price"] = int(out["price"])
    return out


def export_config(client: KomariClient, include_secrets: bool = False) -> dict[str, Any]:
    pub = client.get_public()
    raw_settings = client.get_settings()
    raw_nodes = client.list_clients()
    raw_pings = client.list_ping_tasks()

    site_cfg = {k: raw_settings[k] for k in SAFE_SITE_KEYS if k in raw_settings}

    active_theme = pub.get("theme") or raw_settings.get("theme") or "default"
    raw_theme_settings = pub.get("theme_settings")
    clean_theme_settings = {}
    if isinstance(raw_theme_settings, dict):
        clean_theme_settings = {
            k: v for k, v in sorted(raw_theme_settings.items()) if k not in INTERNAL_THEME_KEYS
        }

    uuid_to_name = {n["uuid"]: n["name"] for n in raw_nodes if n.get("uuid") and n.get("name")}
    sorted_nodes = sorted(raw_nodes, key=lambda n: (int(n.get("weight") or 0), str(n.get("name") or "")))
    nodes_out: list[dict[str, Any]] = []
    for raw in sorted_nodes:
        norm = canonicalize_node(raw)
        entry: dict[str, Any] = {"name": norm["name"]}
        if include_secrets:
            entry["uuid"] = raw.get("uuid", "")
            entry["token"] = raw.get("token", "")
        for key in EDITABLE_NODE_KEYS[1:]:
            val = norm[key]
            if key == "traffic_limit":
                val = format_byte_size(val)
            entry[key] = val
        nodes_out.append(entry)

    sorted_pings = sorted(raw_pings, key=lambda p: (int(p.get("weight") or 0), int(p.get("id") or 0)))
    pings_out: list[dict[str, Any]] = []
    for p in sorted_pings:
        client_names = [uuid_to_name.get(u, u) for u in (p.get("clients") or [])]
        pings_out.append(
            {
                "name": p.get("name", ""),
                "type": p.get("type", "icmp"),
                "target": p.get("target", ""),
                "interval": int(p.get("interval") or 60),
                "default_on": bool(p.get("default_on", False)),
                "clients": client_names,
            }
        )

    return {
        "version": 1,
        "site": site_cfg,
        "theme": {
            "active": active_theme,
            "settings": clean_theme_settings,
        },
        "nodes": nodes_out,
        "ping_tasks": pings_out,
    }


def apply_config(
    client: KomariClient,
    cfg: dict[str, Any],
    *,
    dry_run: bool = False,
    sections: set[str],
    prune_ping_tasks: bool = False,
    emit_agents: str | None = None,
) -> int:
    changes = 0
    prefix = "[DRY-RUN] " if dry_run else ""

    # 1. Site settings
    if "site" in sections and isinstance(cfg.get("site"), dict):
        live_site = client.get_settings()
        site_diff = {}
        for k, desired in cfg["site"].items():
            if k not in SAFE_SITE_KEYS:
                continue
            if live_site.get(k) != desired:
                site_diff[k] = desired
        if site_diff:
            changes += len(site_diff)
            print(f"{prefix}[site] update keys: {sorted(site_diff.keys())}")
            if not dry_run:
                client.update_settings(site_diff)
        else:
            print("[site] up to date")

    # 2. Nodes
    live_nodes = client.list_clients()
    by_uuid = {n["uuid"]: n for n in live_nodes if n.get("uuid")}
    by_name = {n["name"]: n for n in live_nodes if n.get("name")}
    created_nodes: list[dict[str, str]] = []

    if "nodes" in sections and isinstance(cfg.get("nodes"), list):
        for desired_raw in cfg["nodes"]:
            if not isinstance(desired_raw, dict) or not desired_raw.get("name"):
                continue
            desired = canonicalize_node(desired_raw)
            name = desired["name"]
            existing = None
            if desired_raw.get("uuid") and desired_raw["uuid"] in by_uuid:
                existing = by_uuid[desired_raw["uuid"]]
            elif name in by_name:
                existing = by_name[name]

            if existing is None:
                changes += 1
                print(f"{prefix}[node] CREATE {name} (group={desired['group']!r}, region={desired['region']!r}, price={desired['currency']}{desired['price']})")
                if not dry_run:
                    created = client.add_client(name)
                    new_uuid = created.get("uuid")
                    new_token = created.get("token", "")
                    if not new_uuid:
                        # Refresh list to locate newly created node
                        refreshed = client.list_clients()
                        matched = next((n for n in refreshed if n.get("name") == name), None)
                        if not matched:
                            raise RuntimeError(f"Failed to locate created node {name}")
                        new_uuid = matched["uuid"]
                        new_token = matched.get("token", "")
                    edit_payload = {k: v for k, v in desired.items() if v is not None}
                    client.edit_client(new_uuid, edit_payload)
                    created_nodes.append({"name": name, "uuid": new_uuid, "token": new_token})
                    by_name[name] = {"uuid": new_uuid, "token": new_token, **desired}
                    by_uuid[new_uuid] = by_name[name]
            else:
                live_norm = canonicalize_node(existing)
                diff: dict[str, Any] = {}
                for k in EDITABLE_NODE_KEYS:
                    if k not in desired_raw and k != "name":
                        continue
                    if live_norm.get(k) != desired.get(k):
                        diff[k] = desired.get(k)
                if diff:
                    changes += 1
                    print(f"{prefix}[node] UPDATE {name}: {diff}")
                    if not dry_run:
                        # Komari edit endpoint accepts partial updates alongside uuid
                        payload = {k: v for k, v in diff.items() if v is not None}
                        if payload:
                            client.edit_client(existing["uuid"], payload)
                else:
                    print(f"[node] up to date: {name}")

    # Refresh nodes mapping if mutations occurred
    if not dry_run and ("nodes" in sections):
        live_nodes = client.list_clients()
        by_name = {n["name"]: n for n in live_nodes if n.get("name")}
        by_uuid = {n["uuid"]: n for n in live_nodes if n.get("uuid")}

    # 3. Ping tasks
    if "ping" in sections and isinstance(cfg.get("ping_tasks"), list):
        live_pings = client.list_ping_tasks()
        ping_by_key = {
            (str(p.get("name", "")), str(p.get("type", "")), str(p.get("target", ""))): p
            for p in live_pings
        }
        desired_keys: set[tuple[str, str, str]] = set()
        tasks_to_edit: list[dict[str, Any]] = []

        for task in cfg["ping_tasks"]:
            if not isinstance(task, dict):
                continue
            t_name = str(task.get("name") or "").strip()
            t_type = str(task.get("type") or "icmp").strip()
            t_target = str(task.get("target") or "").strip()
            t_interval = int(task.get("interval") or 60)
            t_default_on = bool(task.get("default_on", False))
            if not t_name or not t_target:
                continue

            resolved_uuids: list[str] = []
            unresolved: list[str] = []
            for ref in task.get("clients") or []:
                ref_str = str(ref)
                if ref_str in by_uuid:
                    resolved_uuids.append(ref_str)
                elif ref_str in by_name:
                    resolved_uuids.append(by_name[ref_str]["uuid"])
                else:
                    unresolved.append(ref_str)
            if unresolved:
                print(f"{prefix}[ping] WARN task {t_name!r} references unknown nodes (skipped): {unresolved}")

            key = (t_name, t_type, t_target)
            desired_keys.add(key)
            existing = ping_by_key.get(key)
            if existing is None:
                changes += 1
                print(f"{prefix}[ping] CREATE {t_name} ({t_type} -> {t_target}, interval={t_interval}s, clients={len(resolved_uuids)})")
                if not dry_run:
                    client.add_ping_task(
                        {
                            "name": t_name,
                            "type": t_type,
                            "target": t_target,
                            "interval": t_interval,
                            "default_on": t_default_on,
                            "clients": resolved_uuids,
                        }
                    )
            else:
                same_clients = sorted(existing.get("clients") or []) == sorted(resolved_uuids)
                same_interval = int(existing.get("interval") or 0) == t_interval
                same_default = bool(existing.get("default_on", False)) == t_default_on
                if not (same_clients and same_interval and same_default):
                    changes += 1
                    print(f"{prefix}[ping] UPDATE {t_name} ({t_type} -> {t_target})")
                    tasks_to_edit.append(
                        {
                            "id": existing["id"],
                            "name": t_name,
                            "type": t_type,
                            "target": t_target,
                            "interval": t_interval,
                            "default_on": t_default_on,
                            "clients": resolved_uuids,
                        }
                    )
                else:
                    print(f"[ping] up to date: {t_name}")

        if tasks_to_edit and not dry_run:
            client.edit_ping_tasks(tasks_to_edit)

        if prune_ping_tasks:
            to_delete = [
                p for k, p in ping_by_key.items() if k not in desired_keys and p.get("id") is not None
            ]
            if to_delete:
                changes += len(to_delete)
                print(f"{prefix}[ping] PRUNE {[p.get('name') for p in to_delete]}")
                if not dry_run:
                    client.delete_ping_tasks([int(p["id"]) for p in to_delete])

    # 4. Theme & Theme Settings
    if "theme" in sections and isinstance(cfg.get("theme"), dict):
        theme_cfg = cfg["theme"]
        pub = client.get_public()
        live_theme = pub.get("theme") or "default"
        desired_theme = str(theme_cfg.get("active") or live_theme)
        installed_shorts = {t.get("short") for t in client.list_themes() if t.get("short")}

        if desired_theme != live_theme:
            if desired_theme not in installed_shorts:
                print(
                    f"{prefix}[theme] WARN theme {desired_theme!r} is not installed yet "
                    f"(installed: {sorted(installed_shorts)}); upload the theme ZIP first."
                )
            else:
                changes += 1
                print(f"{prefix}[theme] ACTIVATE {live_theme!r} -> {desired_theme!r}")
                if not dry_run:
                    client.set_theme(desired_theme)
                    pub = client.get_public()
                    live_theme = desired_theme

        desired_settings = theme_cfg.get("settings")
        if isinstance(desired_settings, dict) and desired_settings:
            target_theme = desired_theme if desired_theme in installed_shorts else live_theme
            live_ts = pub.get("theme_settings") if isinstance(pub.get("theme_settings"), dict) else {}
            diff_keys = [
                k for k, v in desired_settings.items()
                if k not in INTERNAL_THEME_KEYS and live_ts.get(k) != v
            ]
            if diff_keys:
                changes += len(diff_keys)
                print(f"{prefix}[theme] UPDATE settings on {target_theme!r}: {sorted(diff_keys)}")
                if not dry_run:
                    merged = dict(live_ts)
                    for k, v in desired_settings.items():
                        if k not in INTERNAL_THEME_KEYS:
                            merged[k] = v
                    client.update_theme_settings(target_theme, merged)
            else:
                print(f"[theme] settings up to date ({target_theme})")

    if emit_agents:
        endpoint = emit_agents.rstrip("/")
        targets = created_nodes if created_nodes else [
            {"name": n["name"], "token": n.get("token", "")} for n in live_nodes
        ]
        if targets:
            print("\n=== Ready-to-run Docker komari-agent commands ===")
            for item in targets:
                tok = item.get("token") or "<TOKEN>"
                print(
                    f"# {item['name']}\n"
                    f"docker run -d --name komari-agent --restart unless-stopped "
                    f"--network host --pid host --cap-add NET_RAW "
                    f"-v /:/host:ro -v /etc/os-release:/etc/os-release:ro "
                    f"ghcr.io/komari-monitor/komari-agent:latest "
                    f"-e {endpoint} -t {tok} --disable-auto-update\n"
                )

    print(f"\nDone. Total pending/applied changes: {changes}")
    return changes


def render_probe_targets(cfg: dict[str, Any]) -> str:
    probe = cfg.get("mainland_probe")
    if not isinstance(probe, dict) or not isinstance(probe.get("targets"), list):
        raise ValueError("Config file does not contain a 'mainland_probe.targets' list")
    return json.dumps(probe["targets"], ensure_ascii=False, separators=(",", ":"))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--url",
        default=os.environ.get("KOMARI_URL", ""),
        help="Komari base URL, e.g. http://127.0.0.1:25774 (env: KOMARI_URL)",
    )
    common.add_argument(
        "-u",
        "--username",
        default=os.environ.get("KOMARI_USERNAME", "admin"),
        help="Komari admin username (env: KOMARI_USERNAME, default: admin)",
    )
    common.add_argument(
        "-p",
        "--password",
        default=os.environ.get("KOMARI_PASSWORD", ""),
        help="Komari admin password (env: KOMARI_PASSWORD; prompts if omitted)",
    )
    common.add_argument(
        "--format",
        choices=("yaml", "json"),
        default=None,
        help="Override file format (default: inferred from extension)",
    )

    parser = argparse.ArgumentParser(
        description="Declarative YAML/JSON export & import tool for Komari Monitor."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_exp = sub.add_parser("export", parents=[common], help="Export live Komari config to YAML/JSON")
    p_exp.add_argument("-o", "--output", required=True, help="Output .yml/.yaml/.json path")
    p_exp.add_argument(
        "--include-secrets",
        action="store_true",
        help="Include node uuid and agent token (keep file chmod 600 and out of public Git!)",
    )

    p_app = sub.add_parser("apply", parents=[common], help="Idempotently apply YAML/JSON config to Komari")
    p_app.add_argument("-f", "--file", required=True, help="Input .yml/.yaml/.json path")
    p_app.add_argument("--dry-run", action="store_true", help="Show planned changes without modifying server")
    p_app.add_argument(
        "--only",
        default="site,nodes,ping,theme",
        help="Comma-separated sections to sync: site,nodes,ping,theme",
    )
    p_app.add_argument(
        "--prune-ping-tasks",
        action="store_true",
        help="Delete ping tasks that exist on server but not in config file",
    )
    p_app.add_argument(
        "--emit-agents",
        default=None,
        help="Print ready-to-paste Docker komari-agent commands pointing to this endpoint URL",
    )

    p_diff = sub.add_parser("diff", parents=[common], help="Alias for 'apply --dry-run'")
    p_diff.add_argument("-f", "--file", required=True, help="Input .yml/.yaml/.json path")
    p_diff.add_argument("--only", default="site,nodes,ping,theme", help="Sections to compare")
    p_diff.add_argument("--prune-ping-tasks", action="store_true", help="Include ping task deletions in diff")

    p_probe = sub.add_parser("render-probe", help="Output PROBE_TARGETS JSON from mainland_probe.targets")
    p_probe.add_argument("-f", "--file", required=True, help="Input .yml/.yaml/.json path")
    p_probe.add_argument("--format", choices=("yaml", "json"), default=None)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "render-probe":
        cfg = load_config_file(args.file, args.format)
        print(render_probe_targets(cfg))
        return 0

    if not args.url:
        parser.error("--url (or KOMARI_URL) is required")
    password = args.password or getpass.getpass(f"Komari password for {args.username}@{args.url}: ")

    client = KomariClient(args.url)
    client.login(args.username, password)

    if args.command == "export":
        data = export_config(client, include_secrets=args.include_secrets)
        dump_config_file(data, args.output, args.format)
        if args.include_secrets:
            try:
                os.chmod(args.output, 0o600)
            except OSError:
                pass
        print(
            f"Exported {len(data['nodes'])} nodes, {len(data['ping_tasks'])} ping tasks, "
            f"theme={data['theme']['active']!r} -> {args.output}"
        )
        return 0

    cfg = load_config_file(args.file, args.format)
    sections = {s.strip().lower() for s in args.only.split(",") if s.strip()}
    dry_run = args.command == "diff" or bool(getattr(args, "dry_run", False))
    apply_config(
        client,
        cfg,
        dry_run=dry_run,
        sections=sections,
        prune_ping_tasks=bool(getattr(args, "prune_ping_tasks", False)),
        emit_agents=getattr(args, "emit_agents", None),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
