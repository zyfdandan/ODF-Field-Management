#!/usr/bin/env python3
"""Remove orphan 轧钢调度室 rear port and TMP placeholders on dispatch link ODFs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "field_portal"))

from import_from_excel import NetBoxClient
from reset_fms_data import paginate

DEFAULT_CONFIG = ROOT / "netbox_config.json"

DISPATCH_ODFS = ("轧钢机房至轧钢调度室", "轧钢调度室至轧钢机房")
ORPHAN_REAR = "轧钢调度室至轧钢机房"  # duplicate on 轧钢调度室 side, 0 front ports, not on cable


def delete_rear_port(client: NetBoxClient, device_id: int, name: str, *, dry_run: bool) -> bool:
    hits = client.request("GET", "/dcim/rear-ports/", params={"device_id": device_id, "name": name, "limit": 5})
    removed = False
    for rp in hits.get("results", []):
        rp_full = client.request("GET", f"/dcim/rear-ports/{rp['id']}/")
        front = len(rp_full.get("front_ports") or [])
        cable = rp_full.get("cable") or []
        if cable:
            print(f"  SKIP rear {name!r} id={rp['id']}: still on cable")
            continue
        if front:
            print(f"  SKIP rear {name!r} id={rp['id']}: has {front} front ports")
            continue
        print(f"  DELETE rear {name!r} id={rp['id']}")
        if not dry_run:
            client.request("DELETE", f"/dcim/rear-ports/{rp['id']}/")
        removed = True
    return removed


def delete_tmp_rears(client: NetBoxClient, device_id: int, *, dry_run: bool) -> int:
    n = 0
    for rp in paginate(client, "/dcim/rear-ports/", {"device_id": device_id}):
        name = (rp.get("name") or "").strip()
        if not name.upper().startswith("TMP-"):
            continue
        rp_full = client.request("GET", f"/dcim/rear-ports/{rp['id']}/")
        if rp_full.get("cable") or rp_full.get("front_ports"):
            print(f"  SKIP TMP {name!r} id={rp['id']}: in use")
            continue
        print(f"  DELETE TMP rear {name!r} id={rp['id']}")
        if not dry_run:
            client.request("DELETE", f"/dcim/rear-ports/{rp['id']}/")
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    client = NetBoxClient(cfg["base_url"], cfg["token"], cfg.get("verify_ssl", False))

    print("=== 轧钢调度室链路清理 ===")
    for dev_name in DISPATCH_ODFS:
        hits = client.request("GET", "/dcim/devices/", params={"name": dev_name, "limit": 1})
        if not hits.get("results"):
            print(f"设备未找到: {dev_name}")
            continue
        dev = hits["results"][0]
        print(f"\n{dev_name} id={dev['id']}:")
        if dev_name == "轧钢调度室至轧钢机房":
            delete_rear_port(client, dev["id"], ORPHAN_REAR, dry_run=args.dry_run)
        delete_tmp_rears(client, dev["id"], dry_run=args.dry_run)

    print("\n保留: 缆段「轧钢机房至轧钢调度室」及两端 ODF（轧钢机房↔轧钢调度室 24芯）")
    if args.dry_run:
        print("(dry-run，未实际删除)")


if __name__ == "__main__":
    main()
