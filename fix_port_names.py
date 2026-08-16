#!/usr/bin/env python3
import re
from bootstrap_test_infra import API, DEVICES, rename_front_ports

api = API()
for name in DEVICES:
    hits = api.req("GET", "/dcim/devices/", params={"name": name, "limit": 1})
    if not hits.get("results"):
        print(f"缺失: {name}")
        continue
    did = hits["results"][0]["id"]
    fps = api.req("GET", "/dcim/front-ports/", params={"device_id": did, "limit": 200})
    for fp in fps.get("results", []):
        n = str(fp["name"])
        m = re.match(r"^F(\d+)$", n)
        if m:
            nn = f"{m.group(1)}-{m.group(1)}"
            api.req("PATCH", f"/dcim/front-ports/{fp['id']}/", json={"name": nn})
    rename_front_ports(api, did)
    sample = api.req("GET", "/dcim/front-ports/", params={"device_id": did, "limit": 5})
    print(name, [x["name"] for x in sample.get("results", [])[:5]])
