#!/usr/bin/env python3
import json
from pathlib import Path

import requests

cfg = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
s = requests.Session()
s.headers.update({"Authorization": f"Token {cfg['token']}", "Accept": "application/json"})
base = cfg["base_url"].rstrip("/")

for pid in [1, 2, 3]:
    p = s.get(f"{base}/plugins/fms/fiber-circuit-paths/{pid}/").json()
    t = s.get(f"{base}/plugins/fms/fiber-circuit-paths/{pid}/trace/").json()
    Path(f"trace_path_{pid}.json").write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== path {pid} circuit={t.get('circuit_name')} complete={t.get('is_complete')} ===")
    print(f"  hops={len(t.get('hops', []))} path_hops={len(p.get('path') or [])}")
    for i, hop in enumerate(t.get("hops", [])):
        keys = sorted(hop.keys())
        print(f"  hop {i} type={hop.get('type')} keys={keys}")
