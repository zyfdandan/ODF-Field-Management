#!/usr/bin/env python3
import json
import re
from pathlib import Path

import requests

cfg = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers["Authorization"] = f"Token {cfg['token']}"
web = cfg["web_base_url"].rstrip("/")

for pid in [1, 3]:
    url = f"{web}/plugins/fms/fiber-circuit-paths/{pid}/"
    r = s.get(url, verify=False)
    print(f"=== web path {pid} status={r.status_code} len={len(r.text)} ===")
    text = r.text
    for pat in [
        r"trace[^\"']*\.js",
        r"cytoscape",
        r"fa-spinner",
        r"spinner",
        r"trace-data",
        r"traceData",
        r"fetchTrace",
        r"renderTrace",
        r"htmx",
        r"/trace/",
    ]:
        hits = re.findall(pat, text, re.I)
        if hits:
            print(f"  {pat}: {len(hits)} hits, sample={hits[:3]}")
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', text)
    print("  scripts:", scripts[-8:])
    Path(f"web_path_{pid}.html").write_text(text, encoding="utf-8")
