#!/usr/bin/env python3
"""Full reset NetBox fiber test lab and re-bootstrap with new naming rules."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from bootstrap_test_infra import main as bootstrap_infra
from import_from_excel import import_circuits

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "field_portal" / "circuit_store.json"
DEFAULT_EXCEL = ROOT / "现场登记_new.xlsx"


def clear_portal_store() -> None:
    STORE.write_text("{}\n", encoding="utf-8")
    print(f"Cleared {STORE}")


def generate_excel() -> Path:
    subprocess.run([sys.executable, str(ROOT / "generate_unified.py")], check=True, cwd=ROOT)
    return DEFAULT_EXCEL


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset NetBox + bootstrap + import test circuits")
    parser.add_argument("--skip-import", action="store_true", help="仅重置并建基础设施，不导入 Excel 光路")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        subprocess.run([sys.executable, str(ROOT / "reset_all_infra.py"), "--dry-run"], check=True, cwd=ROOT)
        return

    print("==> 1/4 清除 NetBox 全部光纤调试数据")
    subprocess.run([sys.executable, str(ROOT / "reset_all_infra.py")], check=True, cwd=ROOT)

    print("\n==> 2/4 清除 Portal 登记缓存")
    clear_portal_store()

    print("\n==> 3/4 重建 DCIM + FMS 基础设施（新命名）")
    bootstrap_infra()

    if args.skip_import:
        print("\n跳过 Excel 导入。如需光路测试：python generate_unified.py && python import_from_excel.py")
        return

    print("\n==> 4/4 生成并导入测试 Excel")
    excel = generate_excel()
    config = json.loads((ROOT / "netbox_config.json").read_text(encoding="utf-8"))
    import_circuits(excel, config, dry_run=False, skip_infra=True, link_mode="port")
    print("\n全部完成。")


if __name__ == "__main__":
    main()
