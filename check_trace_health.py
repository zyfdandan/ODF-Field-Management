#!/usr/bin/env python3
"""Verify FMS Trace UI prerequisites on NetBox."""
import json
import sys
from pathlib import Path

import requests

cfg = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
web = cfg["web_base_url"].rstrip("/")
api = cfg["base_url"].rstrip("/")
headers = {"Authorization": f"Token {cfg['token']}", "Accept": "application/json"}

STATIC_FILES = [
    "/static/netbox_fms/dist/trace-view.min.js",
    "/static/netbox_fms/css/fms-components.css",
]

issues = []

for path in STATIC_FILES:
    r = requests.get(web + path, timeout=15, verify=False)
    ok = r.status_code == 200 and len(r.content) > 100
    print(f"{'OK' if ok else 'FAIL'} {path} -> HTTP {r.status_code} ({len(r.content)} bytes)")
    if not ok:
        issues.append(f"Missing static file: {path} (run collectstatic on NetBox)")

paths = requests.get(f"{api}/plugins/fms/fiber-circuit-paths/", headers=headers, params={"limit": 20}, verify=False)
paths.raise_for_status()
for p in paths.json()["results"]:
    pid = p["id"]
    t = requests.get(f"{api}/plugins/fms/fiber-circuit-paths/{pid}/trace/", headers=headers, verify=False)
    data = t.json()
    hops = len(data.get("hops") or [])
    complete = data.get("is_complete")
    name = data.get("circuit_name", pid)
    status = "OK" if complete and hops else "WARN"
    print(f"{status} path {pid} {name}: complete={complete} trace_hops={hops}")
    if not complete or hops == 0:
        issues.append(f"Path {pid} ({name}) incomplete or empty trace")

if issues:
    print("\nIssues found:")
    for item in issues:
        print(" -", item)
    if any("Missing static" in i for i in issues):
        print("\nFix: on NetBox host run:")
        print("  bash C:/Users/Administrator/netbox-docker-patches/deploy_fms_static.sh")
        print("Or deploy template patch from netbox-docker-patches/netbox_fms/templates/")
    sys.exit(1)

print("\nAll trace checks passed.")
