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
fps = s.get(base + "/dcim/front-ports/", params={"device_id": did, "limit": 200}).json()["results"]
by_id = {fp["id"]: fp for fp in fps}
for fp in sorted(fps, key=lambda x: x["name"]):
    if fp["name"] in ("1-1", "2-2", "c2-1-1", "c2-2-2") or fp["id"] in (13, 10):
        print(fp["id"], fp["name"], "cable", fp.get("cable"), "link_peers", fp.get("link_peers"))

cables = s.get(base + "/dcim/cables/", params={"limit": 200}).json()["results"]
for cab in cables:
    for side in ("a_terminations", "b_terminations"):
        for t in cab.get(side) or []:
            if t.get("object_type") == "dcim.frontport" and t.get("object_id") in by_id:
                print(f"CABLE {cab['id']} {cab.get('label')} -> {by_id[t['object_id']]['name']}")
