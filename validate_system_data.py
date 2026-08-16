#!/usr/bin/env python3
"""Compare 公司光缆资产_导入确认表.xlsx vs NetBox vs Field Portal tree."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "field_portal"))

from import_asset_infra import DEFAULT_XLSX, parse_workbook
from import_from_excel import NetBoxClient
from field_service import is_odf_device, paginate
from field_browser import get_location_tree, list_all_odf_records
from naming_rules import is_bootstrap_tmp_cable, is_odf_device_name


def main() -> None:
    cfg = json.loads((ROOT / "netbox_config.json").read_text(encoding="utf-8"))
    client = NetBoxClient(cfg["base_url"], cfg["token"], cfg.get("verify_ssl", False))
    data = parse_workbook(DEFAULT_XLSX)

    sheet_sites = {s["name"] for s in data["sites"]}
    sheet_locs = {l["name"] for l in data["locations"]}
    sheet_odfs = {d["name"] for d in data["devices"]}
    sheet_trunks = {t["label"] for t in data["trunks"]}

    nb_sites = {d["name"] for d in paginate(client, "/dcim/sites/")}
    nb_locs = {d["name"] for d in paginate(client, "/dcim/locations/")}
    nb_odf_devs = {d["name"]: d for d in paginate(client, "/dcim/devices/") if is_odf_device(d)}
    nb_odfs = set(nb_odf_devs)
    nb_cables = {d["label"] for d in paginate(client, "/dcim/cables/") if d.get("label")}

    print("=== 数量对比 ===")
    print(f"确认表: 站点={len(sheet_sites)} 机房={len(sheet_locs)} ODF={len(sheet_odfs)} 缆段={len(sheet_trunks)}")
    print(f"NetBox: 站点={len(nb_sites)} 机房={len(nb_locs)} ODF={len(nb_odfs)} 缆段={len(nb_cables)}")

    missing_odf = sorted(sheet_odfs - nb_odfs)
    extra_odf = sorted(nb_odfs - sheet_odfs)
    missing_trunk = sorted(sheet_trunks - nb_cables)
    extra_cable = sorted(c for c in nb_cables - sheet_trunks if not is_bootstrap_tmp_cable(c))

    print(f"\n=== ODF 确认表有、NetBox 无 ({len(missing_odf)}) ===")
    for x in missing_odf[:15]:
        print(f"  - {x}")
    if len(missing_odf) > 15:
        print(f"  ... 另有 {len(missing_odf) - 15} 条")

    print(f"\n=== ODF NetBox 多出的 ({len(extra_odf)}) ===")
    for x in extra_odf[:15]:
        print(f"  - {x}")

    print(f"\n=== 缆段 确认表有、NetBox 无 ({len(missing_trunk)}) ===")
    for x in missing_trunk[:15]:
        print(f"  - {x}")

    print(f"\n=== 缆段 NetBox 多出的非 TMP ({len(extra_cable)}) ===")
    for x in extra_cable[:15]:
        print(f"  - {x}")

    odf_by_name = {d["name"]: d for d in data["devices"]}
    loc_mismatch: list[tuple[str, str, str]] = []
    for name, sd in odf_by_name.items():
        nd = nb_odf_devs.get(name)
        if not nd:
            continue
        exp_loc = sd.get("location", "")
        act_loc = (nd.get("location") or {}).get("name", "") if isinstance(nd.get("location"), dict) else ""
        if exp_loc and act_loc and exp_loc != act_loc:
            loc_mismatch.append((name, exp_loc, act_loc))
    print(f"\n=== ODF 机房位置不一致 ({len(loc_mismatch)}) ===")
    for name, exp, act in loc_mismatch[:15]:
        print(f"  - {name}: 表={exp} NetBox={act}")

    trunk_ep_mismatch: list[tuple[str, list[str], list[str]]] = []
    for t in data["trunks"]:
        label = t["label"]
        hits = client.request("GET", "/dcim/cables/", params={"label": label, "limit": 1})
        if not hits.get("results"):
            continue
        cable = hits["results"][0]
        devs: set[str] = set()
        for side in ("a_terminations", "b_terminations"):
            for term in cable.get(side) or []:
                obj = term.get("object") or {}
                dn = (obj.get("device") or {}).get("name")
                if dn:
                    devs.add(dn)
        exp = {t["dev_a"], t["dev_b"]}
        if devs != exp:
            trunk_ep_mismatch.append((label, sorted(exp), sorted(devs)))
    print(f"\n=== 缆段两端 ODF 与确认表不一致 ({len(trunk_ep_mismatch)}) ===")
    for label, exp, act in trunk_ep_mismatch[:15]:
        print(f"  - {label}: 表={exp} NetBox={act}")

    # ODF device type vs strand count
    type_mismatch: list[tuple[str, str, int]] = []
    trunk_strands = {t["label"]: int(t.get("strands") or 12) for t in data["trunks"]}
    for t in data["trunks"]:
        for dev_name in (t["dev_a"], t["dev_b"]):
            nd = nb_odf_devs.get(dev_name)
            if not nd:
                continue
            dt = nd.get("device_type") or {}
            model = dt.get("model", "") if isinstance(dt, dict) else ""
            expected = trunk_strands.get(t["label"], 12)
            if model.startswith("ODF-") and model[4:].isdigit():
                actual = int(model[4:])
                if actual != expected:
                    type_mismatch.append((dev_name, model, expected))
    type_mismatch = sorted(set(type_mismatch))
    print(f"\n=== ODF 型号芯数与缆段不一致 ({len(type_mismatch)}) ===")
    for name, model, exp in type_mismatch[:15]:
        print(f"  - {name}: {model} (缆段需 {exp}芯)")

    tree = get_location_tree(client, force_refresh=True)
    records = list_all_odf_records(client)
    print(f"\n=== Field Portal ===")
    print(f"list_all_odf_records={len(records)} tree.odf_count={tree['odf_count']} tree.route_count={tree['route_count']}")
    if len(records) != len(nb_odfs):
        print(f"  WARN: Portal ODF 数 {len(records)} != NetBox {len(nb_odfs)}")

    # per-room: portal routes vs ODF devices in room
    room_odf_count: dict[str, int] = {}
    for r in records:
        room_odf_count[r["room"]] = room_odf_count.get(r["room"], 0) + 1
    room_route_count: dict[str, int] = {}
    for site in tree["sites"]:
        for room in site["rooms"]:
            room_route_count[room["name"]] = len(room["odfs"])

    route_room_mismatch = []
    for room, odf_n in sorted(room_odf_count.items()):
        route_n = room_route_count.get(room, 0)
        if route_n != odf_n:
            route_room_mismatch.append((room, odf_n, route_n))
    print(f"\n=== 机房 ODF 数 vs Portal 路由数不一致 ({len(route_room_mismatch)}) ===")
    for room, odf_n, route_n in route_room_mismatch[:20]:
        print(f"  - {room}: ODF={odf_n} 路由={route_n}")

    issues = (
        len(missing_odf)
        + len(missing_trunk)
        + len(loc_mismatch)
        + len(trunk_ep_mismatch)
        + len(type_mismatch)
    )
    print(f"\n=== 汇总 ===")
    if issues == 0 and len(extra_odf) == 0 and len(extra_cable) == 0:
        print("确认表与 NetBox 核心数据一致（ODF/缆段/位置/端点）。")
    else:
        print(f"发现 {issues} 处与确认表不一致（不含 NetBox 多余项）。")
    print(f"纤芯校验请见 verify_strands.py 输出。")


if __name__ == "__main__":
    main()
