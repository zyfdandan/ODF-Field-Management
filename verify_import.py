#!/usr/bin/env python3
import json
from pathlib import Path
import requests

c = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers.update({"Authorization": f"Token {c['token']}", "Accept": "application/json"})
base = c["base_url"].rstrip("/")
web = c["web_base_url"].rstrip("/")

circuits = s.get(f"{base}/plugins/fms/fiber-circuits/", params={"limit": 20}).json()["results"]
print(f"circuits: {len(circuits)}")
for circ in circuits:
    paths = s.get(f"{base}/plugins/fms/fiber-circuit-paths/", params={"circuit_id": circ["id"], "limit": 5}).json()["results"]
    p = paths[0] if paths else {}
    print(f"  {circ['cid']} complete={p.get('is_complete')} hops={len(p.get('path') or [])} trace={web}/plugins/fms/fiber-circuit-paths/{p.get('id')}/#trace")
