#!/usr/bin/env python3
"""Provision the Blast Radius Zero demo VM on Vultr.

    VULTR_API_KEY=... ./deploy/vultr_provision.py --region icn --plan vc2-2c-2gb

Creates (idempotently-ish):
  * a firewall group: 22 from the caller's public IP, 80/443 from anywhere
  * an SSH key derived from ~/.ssh/id_ed25519.pub
  * an Ubuntu 24.04 instance bootstrapped with cloud-init, which clones the
    repo and runs deploy/install.sh with a <ip>.sslip.io domain for TLS

Prints the instance id, IP and public URL. Nothing is written to disk except
the returned ids (stdout); the API key stays in the environment.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.vultr.com/v2"
REPO = "https://github.com/coldall1304/blast-radius-zero.git"
BOOTSTRAP = """#!/bin/bash
set -euo pipefail
exec > /var/log/brz-bootstrap.log 2>&1
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl
rm -rf /opt/brz-src
git clone --depth 1 {repo} /opt/brz-src
cd /opt/brz-src
PUBLIC_IP="$(curl -fsS --retry 5 https://api.ipify.org)"
DOMAIN="${{PUBLIC_IP}}.sslip.io"
echo "$DOMAIN" > /opt/brz-domain
BRZ_DOMAIN="$DOMAIN" bash deploy/install.sh
"""


def key() -> str:
    k = os.environ.get("VULTR_API_KEY", "").strip()
    if not k:
        sys.exit("VULTR_API_KEY is not set")
    return k


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise SystemExit(f"Vultr API {method} {path} -> {exc.code}: {detail}") from exc


def public_ip() -> str:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=15) as r:
            return r.read().decode().strip()
    except Exception:
        return "0.0.0.0"


def ensure_firewall(ip: str) -> str:
    label = "brz-demo"
    for group in call("GET", "/firewalls").get("firewall_groups", []):
        if group.get("description") == label:
            return group["id"]
    fw = call("POST", "/firewalls", {"description": label})["firewall_group"]
    rules = [
        ("22", "tcp", ip, 32 if ":" not in ip else 128),
        ("80", "tcp", "0.0.0.0", 0),
        ("443", "tcp", "0.0.0.0", 0),
    ]
    for port, proto, subnet, size in rules:
        call("POST", f"/firewalls/{fw['id']}/rules", {
            "ip_type": "v4", "protocol": proto, "port": port,
            "subnet": subnet, "subnet_size": size, "action": "accept",
        })
    return fw["id"]


def ensure_ssh_key() -> str:
    pub_path = Path.home() / ".ssh" / "id_ed25519.pub"
    if not pub_path.exists():
        sys.exit(f"missing SSH public key: {pub_path}")
    pub = pub_path.read_text().strip()
    name = "brz-" + pub.split()[-1] if len(pub.split()) > 2 else "brz-demo"
    for k in call("GET", "/ssh-keys").get("ssh_keys", []):
        if k.get("ssh_key", "").strip() == pub:
            return k["id"]
    return call("POST", "/ssh-keys", {"name": name, "ssh_key": pub})["ssh_key"]["id"]


def resolve_os() -> str:
    for os_ in call("GET", "/os").get("os", []):
        if os_.get("name") == "Ubuntu 24.04 LTS x64":
            return str(os_["id"])
    sys.exit("Ubuntu 24.04 LTS x64 not found in /os")


def pick_plan(preferred: str, region: str) -> str:
    plans = call("GET", f"/plans?type=vc2&per_page=500").get("plans", [])
    available = [p for p in plans if region in (p.get("locations") or [])]
    names = {p["id"] for p in available}
    if preferred in names:
        return preferred
    for p in sorted(available, key=lambda p: p["monthly_cost"]):
        if p["ram"] >= 2048 and p["vcpu_count"] >= 2:
            return p["id"]
    sys.exit(f"no suitable plan in {region}: {sorted(names)[:10]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="icn")
    ap.add_argument("--plan", default="vc2-2c-2gb")
    ap.add_argument("--label", default="blast-radius-zero")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ip = public_ip()
    domain = os.environ.get("BRZ_DOMAIN", "")  # informational only; bootstrap derives <ip>.sslip.io

    plan = pick_plan(args.plan, args.region) if not args.dry_run else args.plan
    os_id = resolve_os() if not args.dry_run else "2284"
    fw = ensure_firewall(ip) if not args.dry_run else "fw-placeholder"
    ssh_key = ensure_ssh_key() if not args.dry_run else "key-placeholder"

    user_data = (
        "#cloud-config\npackage_update: true\n"
        "write_files:\n"
        "  - path: /opt/brz-bootstrap.sh\n"
        "    permissions: '0755'\n"
        f"    content: |\n" + "\n".join(
            "      " + line for line in BOOTSTRAP.format(repo=REPO, domain=domain).splitlines()
        ) + "\nruncmd:\n  - bash /opt/brz-bootstrap.sh\n"
    )

    body = {
        "region": args.region,
        "plan": plan,
        "os_id": int(os_id),
        "label": args.label[:60],
        "hostname": "blast-radius-zero",
        "firewall_group_id": fw,
        "sshkey_id": [ssh_key],
        "user_data": user_data,
        "backups": "disabled",
        "enable_ipv6": True,
    }
    if args.dry_run:
        print(json.dumps({"would_create": body, "domain": domain}, indent=2)[:2000])
        return

    inst = call("POST", "/instances", body)["instance"]
    iid = inst["id"]
    print(f"instance {iid} created in {args.region} ({plan}); waiting for IP…")

    ip_addr = ""
    for _ in range(90):
        time.sleep(5)
        cur = call("GET", f"/instances/{iid}")["instance"]
        ip_addr = cur.get("main_ip", "")
        if ip_addr and ip_addr != "0.0.0.0" and cur.get("status") == "active":
            break
    if not ip_addr or ip_addr == "0.0.0.0":
        sys.exit(f"instance {iid} never got an IP; check the console")

    print(json.dumps({
        "instance_id": iid,
        "ip": ip_addr,
        "url": f"https://{ip_addr}.sslip.io",
        "ssh": f"root@{ip_addr}",
        "bootstrap_log": "/var/log/brz-bootstrap.log",
    }, indent=2))


if __name__ == "__main__":
    main()
