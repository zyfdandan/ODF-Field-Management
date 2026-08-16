#!/usr/bin/env python3
import json
from pathlib import Path
import requests

c = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers.update({"Authorization": f"Token {c['token']}", "Accept": "application/json"})
base = c["base_url"].rstrip("/")

for fp_id in (1, 2, 53):
    fp = s.get(base + f"/dcim/front-ports/{fp_id}/").json()
    print("FP", fp_id, fp["name"], fp["device"]["name"], "cable", fp.get("cable"), "link_peers", [p.get("name") for p in fp.get("link_peers") or []])

circuits = s.get(base + "/plugins/fms/fiber-circuits/", params={"limit": 20}).json()["results"]
for circ in circuits:
    print("\nCIRCUIT", circ["cid"], "origin", circ.get("origin"), "destination", circ.get("destination"))

paths = s.get(base + "/plugins/fms/fiber-circuit-paths/", params={"limit": 20}).json()["results"]
for p in paths:
    print("PATH", p["id"], "circuit", p.get("circuit"), "origin", p.get("origin"), "destination", p.get("destination"), "complete", p["is_complete"])
