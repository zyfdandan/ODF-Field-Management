#!/usr/bin/env python3
"""Delete all fiber import data: FMS, OSP, DCIM cables/devices/sites (full reset)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from import_from_excel import NetBoxClient
from reset_fms_data import delete_all, delete_osp_data, delete_jump_cables, paginate, purge_fms_nodes_via_docker

DEFAULT_CONFIG = Path(__file__).with_name("netbox_config.json")


def delete_fms_extras(client: NetBoxClient) -> None:
    for label, path in (
        ("FMS 光缆", "/plugins/fms/fiber-cables/"),
        ("FMS 纤芯", "/plugins/fms/fiber-strands/"),
        ("FMS 端口映射", "/plugins/fms/port-mappings/"),
    ):
        delete_all(client, label, path)


def delete_dcim(client: NetBoxClient) -> None:
    delete_all(client, "DCIM 线缆", "/dcim/cables/")
    delete_all(client, "DCIM 前置端口", "/dcim/front-ports/")
    delete_all(client, "DCIM 后置端口", "/dcim/rear-ports/")
    delete_all(client, "DCIM 设备", "/dcim/devices/")
    delete_all(client, "DCIM 位置", "/dcim/locations/")
    delete_all(client, "DCIM 站点", "/dcim/sites/")


def count_all(client: NetBoxClient) -> dict[str, int]:
    keys = {
        "paths": "/plugins/fms/fiber-circuit-paths/",
        "circuits": "/plugins/fms/fiber-circuits/",
        "fms_cables": "/plugins/fms/fiber-cables/",
        "cables": "/dcim/cables/",
        "devices": "/dcim/devices/",
        "sites": "/dcim/sites/",
        "frontports": "/dcim/front-ports/",
    }
    return {k: len(paginate(client, p)) for k, p in keys.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Full reset: FMS + OSP + DCIM sites/devices/cables")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-dcim", action="store_true", help="仅清 FMS/OSP，保留站点设备")
    args = parser.parse_args()

    if not args.config.exists():
        print(f"缺少配置: {args.config}", file=sys.stderr)
        sys.exit(1)

    config = json.loads(args.config.read_text(encoding="utf-8"))
    client = NetBoxClient(config["base_url"], config["token"], config.get("verify_ssl", True))

    print("==> 全量清除调试数据")
    if args.dry_run:
        print("  DRY-RUN:", count_all(client))
        return

    delete_all(client, "光路路径", "/plugins/fms/fiber-circuit-paths/")
    delete_all(client, "光路", "/plugins/fms/fiber-circuits/")
    delete_all(client, "熔接条目", "/plugins/fms/splice-plan-entries/")
    delete_all(client, "熔接方案", "/plugins/fms/splice-plans/")
    delete_fms_extras(client)
    delete_osp_data(client)
    purge_fms_nodes_via_docker()
    delete_jump_cables(client)
    if not args.skip_dcim:
        delete_dcim(client)
    print("完成。", count_all(client))


if __name__ == "__main__":
    main()
