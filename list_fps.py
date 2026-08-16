#!/usr/bin/env python3
import json
from pathlib import Path
import requests

c = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers.update({"Authorization": f"Token {c['token']}", "Accept": "application/json"})
base = c["base_url"].rstrip("/")

for dev in ("轧钢机房-B01-上行ODF框-01", "白灰窑机房-A01-上行ODF框-01", "白灰窑机房-A01-上行ODF框-02"):
    did = s.get(base + "/dcim/devices/", params={"name": dev, "limit": 1}).json()["results"][0]["id"]
    fps = s.get(base + "/dcim/front-ports/", params={"device_id": did, "limit": 200}).json()["results"]
    print(dev)
    for fp in sorted(fps, key=lambda x: x["name"])[:15]:
        print(" ", fp["id"], fp["name"])
