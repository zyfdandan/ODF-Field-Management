#!/usr/bin/env python3
"""Rename legacy F{n} / N-N front ports to ZGtoDF-style 排-芯 names for one ODF."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fiber_infra import FiberInfra
from import_from_excel import NetBoxClient

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "netbox_config.json").read_text(encoding="utf-8"))


def main() -> None:
    odf = sys.argv[1] if len(sys.argv) > 1 else "轧钢机房至得丰机房"
    client = NetBoxClient(CFG["base_url"], CFG["token"], CFG.get("verify_ssl", True))
    infra = FiberInfra(client)
    print(f"==> 重命名 {odf} 前端口")
    infra.fix_device_port_names(odf)
    print("完成")


if __name__ == "__main__":
    main()
