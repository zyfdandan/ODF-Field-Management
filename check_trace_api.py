#!/usr/bin/env python3
import json
from pathlib import Path

import requests

cfg = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
headers = {"Authorization": f"Token {cfg['token']}", "Accept": "application/json"}
base = cfg["base_url"].rstrip("/")
web = cfg["web_base_url"].rstrip("/")

for pid in [1, 2, 3]:
    p = requests.get(f"{base}/plugins/fms/fiber-circuit-paths/{pid}/", headers=headers, verify=False).json()
    r = requests.get(f"{base}/plugins/fms/fiber-circuit-paths/{pid}/trace/", headers=headers, verify=False)
    print(f"=== path {pid} ===")
    print(
        "circuit:",
        p.get("circuit", {}).get("display"),
        "complete:",
        p.get("is_complete"),
        "hops:",
        p.get("hop_count"),
    )
    print("trace status:", r.status_code)
    if r.status_code != 200:
        print(r.text[:500])
        continue
    t = r.json()
    Path(f"trace_path_{pid}.json").write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    print("keys:", sorted(t.keys()))
    for key in ("nodes", "edges", "hops", "devices", "cables"):
        val = t.get(key)
        if isinstance(val, list):
            print(f"  {key}: {len(val)}")
    # Web UI page (non-API)
    r2 = requests.get(
        f"{web}/plugins/fms/fiber-circuit-paths/{pid}/",
        headers={"Authorization": f"Bearer {cfg['token']}", "Accept": "text/html"},
        verify=False,
    )
    print("web page status:", r2.status_code, "len:", len(r2.text))
    print("  has trace-data:", "trace-data" in r2.text or "traceData" in r2.text)
    print("  has spinner:", "spinner" in r2.text or "fa-spinner" in r2.text)
