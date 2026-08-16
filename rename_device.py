#!/usr/bin/env python3
import json
from pathlib import Path

import requests

OLD = "轧钢机房-B01-上行ODF框-01"
NEW = "轧钢机房-JG01-ODF-01"

c = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
h = {"Authorization": f"Token {c['token']}", "Accept": "application/json", "Content-Type": "application/json"}
b = c["base_url"].rstrip("/")

dev = requests.get(f"{b}/dcim/devices/", headers=h, params={"name": OLD, "limit": 1}, verify=False).json()["results"]
if not dev:
    existing = requests.get(f"{b}/dcim/devices/", headers=h, params={"name": NEW, "limit": 1}, verify=False).json()["results"]
    print("already renamed" if existing else "device not found")
else:
    r = requests.patch(f"{b}/dcim/devices/{dev[0]['id']}/", headers=h, json={"name": NEW}, verify=False)
    print(r.status_code, r.json().get("name"))
