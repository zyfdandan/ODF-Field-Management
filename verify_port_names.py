#!/usr/bin/env python3
import json
from pathlib import Path
import requests

c = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers.update({"Authorization": f"Token {c['token']}", "Accept": "application/json"})
base = c["base_url"].rstrip("/")

devs = ["轧钢机房-JG01-ODF-01", "白灰窑机房-A01-ODF-01", "烧结主控机房-A01-ODF-01"]
for name in devs:
    d = s.get(f"{base}/dcim/devices/", params={"name": name, "limit": 1}).json()["results"]
    if not d:
        continue
    fps = s.get(f"{base}/dcim/front-ports/", params={"device_id": d[0]["id"], "limit": 20}).json()["results"]
    names = sorted(p["name"] for p in fps if "to" in p["name"] or "至" in p["name"])[:8]
    cn = [n for n in names if "至" in n]
    print(f"{name}: sample={names[:5]} chinese_left={len(cn)}")

for pid in (7, 8, 9):
    p = s.get(f"{base}/plugins/fms/fiber-circuit-paths/{pid}/").json()
    chain = (p.get("custom_field_data") or {}).get("fiber_chain", "")
    print(f"path {pid} chain={chain} complete={p.get('is_complete')}")
