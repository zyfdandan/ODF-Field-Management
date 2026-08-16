#!/usr/bin/env python3
import json
import time
from pathlib import Path

import requests

c = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers.update({"Authorization": f"Token {c['token']}", "Accept": "application/json"})
base = c["base_url"].rstrip("/")

for path_id in (7, 8, 9):
    t0 = time.time()
    try:
        r = s.get(f"{base}/plugins/fms/fiber-circuit-paths/{path_id}/trace/", timeout=120)
        dt = time.time() - t0
        print(f"path {path_id}: {r.status_code} in {dt:.2f}s len={len(r.text)}")
        if r.ok:
            data = r.json()
            hops = data.get("hops") or []
            print(f"  hops={len(hops)} complete={data.get('is_complete')}")
            for i, h in enumerate(hops[:5]):
                print(f"    {i+1} {h.get('type')} {h.get('name') or h.get('label') or ''}")
            if len(hops) > 5:
                print(f"    ... +{len(hops)-5} more")
        else:
            print(f"  body: {r.text[:500]}")
    except Exception as exc:
        print(f"path {path_id}: ERROR after {time.time()-t0:.2f}s: {exc}")
