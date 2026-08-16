#!/usr/bin/env python3
import json
from pathlib import Path
import requests

c = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers.update({"Authorization": f"Token {c['token']}", "Accept": "application/json"})
base = c["base_url"].rstrip("/")

dev = "白灰窑机房-A01-上行ODF框-01"
did = s.get(base + "/dcim/devices/", params={"name": dev, "limit": 1}).json()["results"][0]["id"]
for name in ("1-1", "c2-1-1"):
    fp = s.get(base + "/dcim/front-ports/", params={"device_id": did, "name": name, "limit": 1}).json()["results"][0]
    print("FP", name, json.dumps({k: fp.get(k) for k in ("id", "module", "device", "name")}, ensure_ascii=False))

mods = s.get(base + "/dcim/modules/", params={"device_id": did, "limit": 10}).json()["results"]
print("MODULES", json.dumps(mods, ensure_ascii=False, indent=2)[:2000])

plans = s.get(base + "/plugins/fms/splice-plans/", params={"limit": 10}).json()["results"]
print("PLANS", plans)
