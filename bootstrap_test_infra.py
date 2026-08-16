#!/usr/bin/env python3
"""Create NetBox DCIM + FMS test infrastructure (portal whitelist routes, 排-芯 naming)."""

from __future__ import annotations

import json
from pathlib import Path

from bootstrap_from_sheet import apply_infra_from_sheet
from fiber_infra import FiberInfra
from import_from_excel import NetBoxClient
from naming_rules import mirror_cable_label
from site_devices import DEV_BHY1, DEV_SJ1, DEV_ZG

CONFIG = json.loads(Path(__file__).with_name("netbox_config.json").read_text(encoding="utf-8"))

# Matches field_portal/portal_config.json allowed routes
SITES = [
    {"name": "轧钢厂", "slug": "zhan-gang-chang"},
    {"name": "烧结厂", "slug": "shao-jie-chang"},
]

DEVICES = [
    {"name": DEV_ZG, "site": "轧钢厂", "location": "轧钢机房"},
    {"name": DEV_BHY1, "site": "烧结厂", "location": "白灰窑机房"},
    {"name": DEV_SJ1, "site": "烧结厂", "location": "烧结主控机房"},
]

TRUNKS = [
    {"label": "CBL-轧钢-白灰窑", "dev_a": DEV_ZG, "dev_b": DEV_BHY1, "strands": "12"},
    {"label": "CBL-白灰窑-烧结", "dev_a": DEV_BHY1, "dev_b": DEV_SJ1, "strands": "12"},
]


def main() -> None:
    client = NetBoxClient(CONFIG["base_url"], CONFIG["token"], CONFIG.get("verify_ssl", True))
    apply_infra_from_sheet(client, SITES, DEVICES, TRUNKS)
    infra = FiberInfra(client)
    print("==> 配置 FMS 端口（两端）")
    for row in TRUNKS:
        label = row["label"]
        mirror = mirror_cable_label(label)
        for dev in (row["dev_a"], row["dev_b"]):
            infra.ensure_module(dev)
        infra.provision_cable(row["dev_a"], label)
        infra.provision_cable(row["dev_b"], mirror)
        infra.fix_cable_termination(label)
        for dev_name, tmp in ((row["dev_a"], f"TMP-{label}-A"), (row["dev_b"], f"TMP-{mirror}-B")):
            hits = client.request("GET", "/dcim/rear-ports/", params={"device_id": infra.dev_id(dev_name), "name": tmp, "limit": 1})
            for rp in hits.get("results", []):
                try:
                    client.request("DELETE", f"/dcim/rear-ports/{rp['id']}/")
                except RuntimeError:
                    pass
    print("==> 纤芯/端口重命名 (排-芯 + 路由前缀)")
    for dev in (DEV_ZG, DEV_BHY1, DEV_SJ1):
        infra.fix_device_port_names(dev)
    print("\n测试基础设施就绪（2 缆段 / 3 ODF / 12芯 1-1..1-12）。")


if __name__ == "__main__":
    main()
