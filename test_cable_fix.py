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

label = "CBL-轧钢-白灰窑01"
cab = s.get(base + "/dcim/cables/", params={"label": label, "limit": 1}).json()["results"][0]
print("before", cab["id"], cab["a_terminations"], cab["b_terminations"])

# provision rear ports
def rp_id(dev, name):
    did = s.get(base + "/dcim/devices/", params={"name": dev, "limit": 1}).json()["results"][0]["id"]
    return s.get(base + "/dcim/rear-ports/", params={"device_id": did, "name": name, "limit": 1}).json()["results"][0]["id"]

a = rp_id("轧钢机房-B01-上行ODF框-01", label)
b = rp_id("白灰窑机房-A01-上行ODF框-01", label)
print("new terminations", a, b)

r = s.patch(
    base + f"/dcim/cables/{cab['id']}/",
    json={
        "a_terminations": [{"object_type": "dcim.rearport", "object_id": a}],
        "b_terminations": [{"object_type": "dcim.rearport", "object_id": b}],
    },
)
print("patch", r.status_code, r.text[:400])
cab = s.get(base + f"/dcim/cables/{cab['id']}/").json()
print("after a", cab["a_terminations"], "b", cab["b_terminations"])

r = s.post(base + "/plugins/fms/fiber-circuits/1/retrace/")
print("retrace", r.status_code)
p = s.get(base + "/plugins/fms/fiber-circuit-paths/1/").json()
print("complete", p["is_complete"], "hops", len(p.get("path") or []))
for i, hop in enumerate(p.get("path") or []):
    print(i, hop)
