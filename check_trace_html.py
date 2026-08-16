#!/usr/bin/env python3
import json
import re
from pathlib import Path

import requests

c = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers["Authorization"] = f"Token {c['token']}"
b = c["base_url"].rstrip("/")

for pid in [1, 3]:
    r = s.get(f"{b}/plugins/fms/fiber-circuit-paths/{pid}/trace/", headers={"Accept": "text/html"})
    print(f"PATH {pid} status={r.status_code} len={len(r.text)}")
    print("  spinner", "spinner" in r.text or "fa-spinner" in r.text)
    print("  cytoscape", "cytoscape" in r.text)
    print("  hops data", "hops" in r.text)
    if r.status_code == 200:
        Path(f"trace_path_{pid}.html").write_text(r.text, encoding="utf-8")
        print(f"  saved trace_path_{pid}.html")
