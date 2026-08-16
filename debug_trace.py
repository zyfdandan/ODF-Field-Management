#!/usr/bin/env python3
import json
from pathlib import Path
import requests

c = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers.update({"Authorization": f"Token {c['token']}", "Accept": "application/json"})
base = c["base_url"].rstrip("/")

dev = s.get(base + "/dcim/devices/", params={"name": "轧钢机房-B01-上行ODF框-01", "limit": 1}).json()["results"][0]
did = dev["id"]
print("DEVICE", did, dev["name"])

for kind in ("front-ports", "rear-ports"):
    ports = s.get(base + f"/dcim/{kind}/", params={"device_id": did, "limit": 20}).json()["results"]
    for p in sorted(ports, key=lambda x: x["name"])[:6]:
        print(kind, p["id"], p["name"], "cable", (p.get("cable") or {}).get("label"), "link", [x.get("name") for x in p.get("link_peers") or []])

# path 1 details
p = s.get(base + "/plugins/fms/fiber-circuit-paths/1/").json()
print("\nPATH1 origin", p.get("origin"), "dest", p.get("destination"))
print("path hops", p.get("path"))

# fix destination - get last port for FIB-轧钢-监控 from excel logic: 烧结 1-1
dest_dev = s.get(base + "/dcim/devices/", params={"name": "烧结主控机房-A01-上行ODF框-01", "limit": 1}).json()["results"][0]
dest_fp = s.get(base + "/dcim/front-ports/", params={"device_id": dest_dev["id"], "name": "1-1", "limit": 1}).json()["results"][0]
print("expected dest", dest_fp["id"], dest_fp["name"], dest_dev["name"])

r = s.patch(base + "/plugins/fms/fiber-circuit-paths/1/", json={"destination": dest_fp["id"]})
print("patch dest", r.status_code)
r = s.post(base + "/plugins/fms/fiber-circuits/1/retrace/")
print("retrace", r.status_code, r.text[:500])
p = s.get(base + "/plugins/fms/fiber-circuit-paths/1/").json()
print("after complete", p["is_complete"], "hops", len(p.get("path") or []))
for i, hop in enumerate(p.get("path") or []):
    print(" ", i, hop)
