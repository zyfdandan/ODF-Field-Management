#!/usr/bin/env python3
import json
from pathlib import Path
import requests

c = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers.update(
    {"Authorization": f"Token {c['token']}", "Accept": "application/json", "Content-Type": "application/json"}
)
base = c["base_url"].rstrip("/")

for name in ("1-1", "c2-1-1", "2-2", "c2-2-2"):
    fp = s.get(base + "/dcim/front-ports/", params={"device_id": 6, "name": name, "limit": 1}).json()["results"][0]
    r = s.patch(base + f"/dcim/front-ports/{fp['id']}/", json={"module": 1})
    print(name, r.status_code, r.text[:200])

r = s.post(
    base + "/plugins/fms/splice-plan-entries/",
    json={"plan": 2, "tray": 1, "fiber_a": 13, "fiber_b": 81, "notes": "test"},
)
print("entry", r.status_code, r.text[:400])
