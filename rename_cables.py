#!/usr/bin/env python3
"""Rename DCIM cable labels (and related rear ports) from old numbered scheme to new."""

from __future__ import annotations

import json
from pathlib import Path

import requests

CONFIG = json.loads(Path(__file__).with_name("netbox_config.json").read_text(encoding="utf-8"))

RENAME_MAP = {
    "CBL-轧钢-白灰窑01": "CBL-轧钢-白灰窑",
    "CBL-白灰窑01-白灰窑02": "CBL-白灰窑-白灰窑2",
    "CBL-白灰窑02-烧结01": "CBL-白灰窑2-烧结",
    "CBL-白灰窑01-烧结01": "CBL-白灰窑-烧结",
    "CBL-烧结02-白灰窑01": "CBL-烧结-白灰窑",
    "CBL-白灰窑01-轧钢": "CBL-白灰窑-轧钢",
}


class API:
    def __init__(self) -> None:
        self.base = CONFIG["base_url"].rstrip("/")
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Token {CONFIG['token']}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self.s.verify = CONFIG.get("verify_ssl", True)

    def req(self, method: str, path: str, **kw):
        url = f"{self.base}{path}"
        r = self.s.request(method, url, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
        return r.json() if r.content else {}


def rename_rear_ports(api: API, device_id: int, old_label: str, new_label: str) -> None:
    for suffix in ("A", "B"):
        old_name = f"RP-{old_label}-{suffix}"
        new_name = f"RP-{new_label}-{suffix}"
        hits = api.req("GET", "/dcim/rear-ports/", params={"device_id": device_id, "name": old_name, "limit": 1})
        if not hits.get("results"):
            continue
        rp = hits["results"][0]
        if rp["name"] != new_name:
            api.req("PATCH", f"/dcim/rear-ports/{rp['id']}/", json={"name": new_name})
            print(f"    后端口: {old_name} -> {new_name}")


def main() -> None:
    api = API()
    print("==> 缆线 label 重命名")
    for old, new in RENAME_MAP.items():
        hits = api.req("GET", "/dcim/cables/", params={"label": old, "limit": 1})
        if not hits.get("results"):
            alt = api.req("GET", "/dcim/cables/", params={"label": new, "limit": 1})
            if alt.get("results"):
                print(f"  跳过（已是新名）: {new}")
            else:
                print(f"  未找到: {old}")
            continue
        cable = hits["results"][0]
        if cable.get("label") == new:
            print(f"  已是: {new}")
            continue
        api.req("PATCH", f"/dcim/cables/{cable['id']}/", json={"label": new})
        print(f"  缆线: {old} -> {new}")
        for side in ("a_terminations", "b_terminations"):
            for term in cable.get(side) or []:
                obj = term.get("object") or {}
                dev = obj.get("device") or {}
                if dev.get("id"):
                    rename_rear_ports(api, dev["id"], old, new)
    print("\n完成。请运行 import_from_excel.py 重命名 ODF 前端口。")


if __name__ == "__main__":
    main()
