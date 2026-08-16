#!/usr/bin/env python3
"""Sync ODF device types (ODF-{芯数}) and rear-port positions to match 光缆缆段 sheet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bootstrap_from_sheet import (
    _ensure_odf_device_type,
    _ensure_rear_port,
    _get_or_create,
    _int_strands,
    _odf_strands_from_trunks,
)
from import_asset_infra import DEFAULT_XLSX, parse_workbook
from import_from_excel import NetBoxClient

DEFAULT_CONFIG = Path(__file__).with_name("netbox_config.json")


def sync_odf_types(client: NetBoxClient, odf_strands: dict[str, int]) -> int:
    mfg = _get_or_create(
        client, "/dcim/manufacturers/", {"name": "Generic"}, {"name": "Generic", "slug": "generic"}
    )
    dtype_cache: dict[int, int] = {}
    updated = 0
    for name, sc in sorted(odf_strands.items()):
        dtype = _ensure_odf_device_type(client, mfg["id"], sc, dtype_cache)
        hits = client.request("GET", "/dcim/devices/", params={"name": name, "limit": 1})
        if not hits.get("results"):
            print(f"  跳过（无设备）: {name}")
            continue
        dev = hits["results"][0]
        cur = dev.get("device_type") or {}
        cur_id = cur["id"] if isinstance(cur, dict) else cur
        if cur_id != dtype["id"]:
            client.request("PATCH", f"/dcim/devices/{dev['id']}/", json={"device_type": dtype["id"]})
            print(f"  型号: {name} -> ODF-{sc}")
            updated += 1
    return updated


def sync_rear_ports(client: NetBoxClient, trunks: list[dict[str, str]]) -> int:
    from fiber_infra import FiberInfra

    infra = FiberInfra(client)
    fixed = 0
    for row in trunks:
        label = row["label"]
        expected = _int_strands(row)
        for dev in (row["dev_a"], row["dev_b"]):
            did = infra.dev_id(dev)
            _ensure_rear_port(client, did, label, expected)
            fixed += 1
    return fixed


def main() -> None:
    ap = argparse.ArgumentParser(description="按缆段芯数同步 ODF 型号与后端口 positions")
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = parse_workbook(args.xlsx)
    odf_strands = _odf_strands_from_trunks(data["trunks"])
    print(f"ODF 芯数映射: {len(odf_strands)} 台")
    for name, sc in sorted(odf_strands.items())[:5]:
        print(f"  {name} -> ODF-{sc}")
    if len(odf_strands) > 5:
        print(f"  ... 共 {len(odf_strands)} 台")

    if args.dry_run:
        return

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    client = NetBoxClient(cfg["base_url"], cfg["token"], cfg.get("verify_ssl", False))

    print("\n==> 更新 ODF 设备型号")
    n = sync_odf_types(client, odf_strands)
    print(f"  更新 {n} 台")

    print("\n==> 更新后端口 positions")
    sync_rear_ports(client, data["trunks"])
    print("  完成")


if __name__ == "__main__":
    main()
