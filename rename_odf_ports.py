#!/usr/bin/env python3
"""Rename ODF front ports to 排-芯 naming after expand_fms_ports_shell.py."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fiber_infra import FiberInfra
from import_asset_infra import DEFAULT_XLSX, parse_workbook
from import_from_excel import NetBoxClient


def main() -> None:
    ap = argparse.ArgumentParser(description="重命名 ODF 前端口为 排-芯 格式")
    ap.add_argument("--config", type=Path, default=Path(__file__).with_name("netbox_config.json"))
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    client = NetBoxClient(cfg["base_url"], cfg["token"], cfg.get("verify_ssl", False))
    infra = FiberInfra(client)
    data = parse_workbook(args.xlsx)

    devs: set[str] = set()
    for row in data["trunks"]:
        devs.add(row["dev_a"])
        devs.add(row["dev_b"])

    print(f"==> 重命名 {len(devs)} 台 ODF 前端口")
    for i, dev in enumerate(sorted(devs), 1):
        try:
            infra.fix_device_port_names(dev)
            if i % 20 == 0 or i == len(devs):
                print(f"  进度 {i}/{len(devs)}")
        except Exception as exc:
            print(f"  ⚠ {dev}: {exc}")
    print("完成。")


if __name__ == "__main__":
    main()
