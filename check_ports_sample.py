"""Sample: list a few front-port names for devices loaded from netbox_config.json."""
import json
from pathlib import Path
from import_from_excel import NetBoxClient

ROOT = Path(__file__).resolve().parent
cfg = json.loads((ROOT / "netbox_config.json").read_text(encoding="utf-8"))
c = NetBoxClient(cfg["base_url"], cfg["token"], cfg.get("verify_ssl", False))

# 改成你环境里的设备名
for name in ("示例ODF-A", "示例ODF-B"):
    results = c.request("GET", "/dcim/devices/", params={"name": name, "limit": 1}).get("results") or []
    if not results:
        print(f"{name}: not found")
        continue
    d = results[0]
    rp = c.request("GET", "/dcim/rear-ports/", params={"device_id": d["id"], "name": name, "limit": 1})
    if not rp.get("results"):
        print(f"{name}: no rear-port")
        continue
    rpd = c.request("GET", f"/dcim/rear-ports/{rp['results'][0]['id']}/")
    fps = rpd.get("front_ports") or []
    samples = []
    for link in fps[:2] + fps[-2:]:
        fp = c.request("GET", f"/dcim/front-ports/{link['front_port']}/")
        samples.append(fp["name"])
    print(f"{name}: {len(fps)} ports, samples={samples}")
