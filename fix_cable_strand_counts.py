#!/usr/bin/env python3
"""Fix FMS strand counts and rear-port positions to match 光缆缆段 sheet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bootstrap_from_sheet import (
    _ensure_fiber_cable_type,
    _ensure_rear_port,
    _get_or_create,
    _parse_trunk_note,
)
from fiber_infra import FiberInfra
from import_asset_infra import DEFAULT_XLSX, parse_workbook
from import_from_excel import NetBoxClient
from reset_fms_data import paginate
from naming_rules import fiber_from_position, mirror_cable_label, port_name_for_cable_fiber

DEFAULT_CONFIG = Path(__file__).with_name("netbox_config.json")


def _int_strands(row: dict[str, str]) -> int:
    try:
        return max(1, int(row.get("strands") or 12))
    except (TypeError, ValueError):
        s, _ = _parse_trunk_note(row.get("note", ""))
        return max(1, int(s or 12))


def _count_strands(client: NetBoxClient, fc_id: int) -> int:
    return len(paginate(client, "/plugins/fms/fiber-strands/", {"fiber_cable_id": fc_id}))


def _delete_strands(client: NetBoxClient, fc_id: int) -> None:
    for s in paginate(client, "/plugins/fms/fiber-strands/", {"fiber_cable_id": fc_id}):
        try:
            client.request("DELETE", f"/plugins/fms/fiber-strands/{s['id']}/")
        except RuntimeError:
            pass


def _create_strands(client: NetBoxClient, fc_id: int, cable_label: str, strand_count: int) -> None:
    from naming_rules import parse_route_label

    route = parse_route_label(cable_label)
    local_a = route["room_a"] if route["room_a"] else cable_label.split("至")[0]
    for pos in range(1, strand_count + 1):
        fiber = fiber_from_position(pos)
        name = port_name_for_cable_fiber(cable_label, fiber, local_room=local_a)
        client.request(
            "POST",
            "/plugins/fms/fiber-strands/",
            json={"fiber_cable": fc_id, "name": name, "position": pos},
        )


def _update_rear_ports(client: NetBoxClient, dev_id: int, names: list[str], positions: int) -> None:
    for name in names:
        if not name:
            continue
        _ensure_rear_port(client, dev_id, name, positions)


def _fc_id_for_label(client: NetBoxClient, label: str) -> tuple[int | None, int | None]:
    hits = client.request("GET", "/dcim/cables/", params={"label": label, "limit": 1})
    if not hits.get("results"):
        return None, None
    cable_id = hits["results"][0]["id"]
    fc_hits = client.request("GET", "/plugins/fms/fiber-cables/", params={"cable_id": cable_id, "limit": 1})
    if not fc_hits.get("results"):
        return cable_id, None
    return cable_id, fc_hits["results"][0]["id"]


def _front_port_count(client: NetBoxClient, device_id: int, rp_name: str) -> tuple[int, int]:
    hits = client.request("GET", "/dcim/rear-ports/", params={"device_id": device_id, "name": rp_name, "limit": 1})
    if not hits.get("results"):
        return 0, 0
    rp = client.request("GET", f"/dcim/rear-ports/{hits['results'][0]['id']}/")
    return int(rp.get("positions") or 0), len(rp.get("front_ports") or [])


def _needs_fix(
    client: NetBoxClient, infra: FiberInfra, row: dict[str, str], fc_id: int | None, expected: int
) -> bool:
    label = row["label"]
    if not fc_id or _count_strands(client, fc_id) != expected:
        return True
    for dev in (row["dev_a"], row["dev_b"]):
        positions, front = _front_port_count(client, infra.dev_id(dev), label)
        if positions != expected or front != expected:
            return True
    return False


def fix_trunk(client: NetBoxClient, infra: FiberInfra, row: dict[str, str], fct_cache: dict[int, int], mfg_id: int) -> str:
    label = row["label"]
    mirror = mirror_cable_label(label)
    expected = _int_strands(row)
    dev_a, dev_b = row["dev_a"], row["dev_b"]
    cable_id, fc_id = _fc_id_for_label(client, label)
    if not cable_id:
        return f"跳过（无 DCIM 缆）: {label}"

    if fc_id and not _needs_fix(client, infra, row, fc_id, expected):
        return f"已正确: {label} ({expected}芯)"

    fct_id = _ensure_fiber_cable_type(client, mfg_id, expected, fct_cache)
    da_id = infra.dev_id(dev_a)
    db_id = infra.dev_id(dev_b)

    _update_rear_ports(
        client,
        da_id,
        [label, f"TMP-{label}-A", f"TMP-{mirror}-B"],
        expected,
    )
    _update_rear_ports(
        client,
        db_id,
        [label, mirror, f"TMP-{mirror}-B", f"TMP-{label}-A"],
        expected,
    )

    if fc_id and _count_strands(client, fc_id) == expected:
        pass
    elif fc_id:
        client.request("PATCH", f"/plugins/fms/fiber-cables/{fc_id}/", json={"fiber_cable_type": fct_id})
        _delete_strands(client, fc_id)
        _create_strands(client, fc_id, label, expected)
    else:
        fc = client.request(
            "POST",
            "/plugins/fms/fiber-cables/",
            json={"cable": cable_id, "fiber_cable_type": fct_id},
        )
        fc_id = fc["id"]
        _create_strands(client, fc_id, label, expected)

    for dev in (dev_a, dev_b):
        try:
            infra.provision_cable(dev, label)
            infra.fix_device_port_names(dev)
        except Exception as exc:
            print(f"    ⚠ 端口 {dev}@{label}: {exc}")
    try:
        infra.fix_cable_termination(label)
    except Exception as exc:
        print(f"    ⚠ 改接 {label}: {exc}")

    got = _count_strands(client, fc_id) if fc_id else 0
    return f"修复: {label} -> {got}/{expected}芯"


def main() -> None:
    ap = argparse.ArgumentParser(description="修正 NetBox 光缆芯数")
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = parse_workbook(args.xlsx)
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    client = NetBoxClient(cfg["base_url"], cfg["token"], cfg.get("verify_ssl", False))
    infra = FiberInfra(client)

    mfg = _get_or_create(
        client, "/dcim/manufacturers/", {"name": "Generic"}, {"name": "Generic", "slug": "generic"}
    )
    fct_cache: dict[int, int] = {}

    print(f"\n==> 修正光缆芯数（共 {len(data['trunks'])} 条）")
    fixed = 0
    for row in data["trunks"]:
        expected = _int_strands(row)
        if args.dry_run:
            print(f"  DRY-RUN {row['label']}: {expected}芯")
            continue
        msg = fix_trunk(client, infra, row, fct_cache, mfg["id"])
        print(f"  {msg}")
        if msg.startswith("修复"):
            fixed += 1
    print(f"\n完成，修复 {fixed} 条。")


if __name__ == "__main__":
    main()
