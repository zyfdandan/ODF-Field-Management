#!/usr/bin/env python3
import json
from pathlib import Path
import requests

c = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers.update({"Authorization": f"Token {c['token']}", "Accept": "application/json"})
base = c["base_url"].rstrip("/")

label = "CBL-轧钢-白灰窑01"
for dev in ("轧钢机房-B01-上行ODF框-01", "白灰窑机房-A01-上行ODF框-01"):
    did = s.get(base + "/dcim/devices/", params={"name": dev, "limit": 1}).json()["results"][0]["id"]
    print(f"\n=== {dev} ===")
    rps = s.get(base + "/dcim/rear-ports/", params={"device_id": did, "limit": 50}).json()["results"]
    for rp in rps:
        if label.replace("CBL-", "") in rp["name"] or label in rp["name"] or "轧钢" in rp["name"]:
            print(
                rp["id"],
                rp["name"],
                "cable",
                (rp.get("cable") or {}).get("label"),
                "peers",
                [p.get("name") for p in rp.get("link_peers") or []],
            )
