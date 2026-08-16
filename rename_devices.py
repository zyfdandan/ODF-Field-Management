#!/usr/bin/env python3
"""Rename ODF devices to unified {机房}-{机柜}-ODF-{序号} scheme."""

from __future__ import annotations

import json
from pathlib import Path

import requests

from site_devices import DEVICE_RENAME_MAP

CONFIG = json.loads(Path(__file__).with_name("netbox_config.json").read_text(encoding="utf-8"))


def main() -> None:
    base = CONFIG["base_url"].rstrip("/")
    h = {
        "Authorization": f"Token {CONFIG['token']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    verify = CONFIG.get("verify_ssl", True)
    print("==> ODF 设备重命名")
    for old, new in DEVICE_RENAME_MAP.items():
        hits = requests.get(
            f"{base}/dcim/devices/", headers=h, params={"name": old, "limit": 1}, verify=verify
        ).json()
        if not hits.get("results"):
            alt = requests.get(
                f"{base}/dcim/devices/", headers=h, params={"name": new, "limit": 1}, verify=verify
            ).json()
            if alt.get("results"):
                print(f"  跳过（已是新名）: {new}")
            else:
                print(f"  未找到: {old}")
            continue
        dev_id = hits["results"][0]["id"]
        r = requests.patch(
            f"{base}/dcim/devices/{dev_id}/", headers=h, json={"name": new}, verify=verify
        )
        if r.status_code >= 400:
            print(f"  失败 {old} -> {new}: {r.status_code} {r.text[:200]}")
        else:
            print(f"  {old} -> {new}")
    print("\n完成。请重新生成 Excel 并 import_from_excel.py。")


if __name__ == "__main__":
    main()
