#!/usr/bin/env python3
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "field_portal")]

from import_asset_infra import DEFAULT_XLSX, parse_workbook
from import_from_excel import NetBoxClient
from field_browser import get_location_tree, list_all_odf_records
from field_service import is_odf_device, paginate

OUT = ROOT / "validate_report.txt"


def main() -> None:
    cfg = json.loads((ROOT / "netbox_config.json").read_text(encoding="utf-8"))
    c = NetBoxClient(cfg["base_url"], cfg["token"], False)
    data = parse_workbook(DEFAULT_XLSX)
    lines: list[str] = []

    def log(msg: str = "") -> None:
        lines.append(msg)

    log("=== NetBox 站点 ===")
    for s in paginate(c, "/dcim/sites/"):
        log(f"  {s['name']}")
    log(f"确认表站点: {[s['name'] for s in data['sites']]}")

    tree = get_location_tree(c, force_refresh=True)
    rec = Counter(r["room"] for r in list_all_odf_records(c))
    log("\n=== 路由数 ≠ ODF 数的机房 ===")
    for site in tree["sites"]:
        for room in site["rooms"]:
            rn = len(room["odfs"])
            on = rec.get(room["name"], 0)
            if rn != on:
                log(f"{room['name']}: ODF={on} routes={rn}")
                for o in room["odfs"]:
                    log(f"  · {o['display_name']}  cable={o.get('cable', '')}")

    log("\n=== 端口命名抽样 ===")
    for name in ["2号门岗至3号门", "得丰机房至轧钢机房", "白灰窑机房至烧结主控机房"]:
        hits = c.request("GET", "/dcim/devices/", params={"name": name, "limit": 1})
        if not hits.get("results"):
            log(f"{name}: 未找到")
            continue
        did = hits["results"][0]["id"]
        fps = sorted(p["name"] for p in paginate(c, "/dcim/front-ports/", {"device_id": did}))[:8]
        log(f"{name}: {fps}")

    pat = re.compile(r"^[A-Za-z0-9]+to[A-Za-z0-9]+\d+-\d+$")
    bad = total = 0
    bad_samples: list[str] = []
    for d in paginate(c, "/dcim/devices/"):
        if not is_odf_device(d):
            continue
        for p in paginate(c, "/dcim/front-ports/", {"device_id": d["id"]}):
            n = p.get("name") or ""
            if not n:
                continue
            total += 1
            if not pat.match(n):
                bad += 1
                if len(bad_samples) < 15:
                    bad_samples.append(f"{d['name']} / {n}")
    log(f"\n=== 前置端口命名 ===")
    log(f"总计 {total}，非标准格式 {bad}")
    for s in bad_samples:
        log(f"  {s}")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
