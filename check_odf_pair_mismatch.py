#!/usr/bin/env python3
"""Compare ODF device types / port counts for each trunk endpoint pair."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from import_asset_infra import DEFAULT_XLSX, parse_workbook
from import_from_excel import NetBoxClient
from naming_rules import is_trunk_cable_label
from reset_fms_data import paginate

OUT = ROOT / "odf_pair_mismatch.txt"


def odf_capacity(device: dict) -> int | None:
    model = ((device.get("device_type") or {}).get("model") or "").strip()
    if model.startswith("ODF-") and model[4:].isdigit():
        return int(model[4:])
    return None


def device_port_summary(client: NetBoxClient, device_id: int) -> dict:
    rears = paginate(client, "/dcim/rear-ports/", {"device_id": device_id})
    trunk_rears = [r for r in rears if is_trunk_cable_label((r.get("name") or "").strip())]
    positions = []
    fronts = 0
    for rp in trunk_rears:
        full = client.request("GET", f"/dcim/rear-ports/{rp['id']}/")
        positions.append(int(full.get("positions") or 0))
        fronts += len(full.get("front_ports") or [])
    all_front = len(paginate(client, "/dcim/front-ports/", {"device_id": device_id}))
    return {
        "trunk_rears": [r.get("name") for r in trunk_rears],
        "rear_positions": positions,
        "trunk_front": fronts,
        "all_front": all_front,
    }


def main() -> None:
    cfg = json.loads((ROOT / "netbox_config.json").read_text(encoding="utf-8"))
    client = NetBoxClient(cfg["base_url"], cfg["token"], False)
    sheet = parse_workbook(DEFAULT_XLSX)

    lines: list[str] = []
    mismatches: list[str] = []

    for row in sheet["trunks"]:
        label = row["label"]
        dev_a = row["dev_a"]
        dev_b = row["dev_b"]
        expected = int(row.get("strands") or 12)

        def load_dev(name: str) -> dict | None:
            hits = client.request("GET", "/dcim/devices/", params={"name": name, "limit": 1})
            return hits["results"][0] if hits.get("results") else None

        da = load_dev(dev_a)
        db = load_dev(dev_b)
        if not da or not db:
            continue
        cap_a, cap_b = odf_capacity(da), odf_capacity(db)
        ps_a = device_port_summary(client, da["id"])
        ps_b = device_port_summary(client, db["id"])

        issue = False
        reasons: list[str] = []
        if cap_a != cap_b:
            issue = True
            reasons.append(f"ODF型号 {dev_a}={cap_a} vs {dev_b}={cap_b}")
        if cap_a != expected or cap_b != expected:
            issue = True
            reasons.append(f"确认表需{expected}芯, 实际 {cap_a}/{cap_b}")
        max_rear_a = max(ps_a["rear_positions"]) if ps_a["rear_positions"] else 0
        max_rear_b = max(ps_b["rear_positions"]) if ps_b["rear_positions"] else 0
        if max_rear_a != max_rear_b:
            issue = True
            reasons.append(f"rear positions {max_rear_a} vs {max_rear_b}")
        if ps_a["all_front"] != ps_b["all_front"]:
            issue = True
            reasons.append(f"front端口总数 {ps_a['all_front']} vs {ps_b['all_front']}")

        if issue:
            mismatches.append(label)
            lines.append(f"=== {label} (表{expected}芯) ===")
            for r in reasons:
                lines.append(f"  · {r}")
            lines.append(f"  A {dev_a}: ODF-{cap_a} rears={ps_a['trunk_rears']} front={ps_a['all_front']}")
            lines.append(f"  B {dev_b}: ODF-{cap_b} rears={ps_b['trunk_rears']} front={ps_b['all_front']}")
            lines.append("")

    lines.insert(0, f"缆段对端 ODF 芯数/端口不一致: {len(mismatches)} / {len(sheet['trunks'])}\n")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"pair mismatches: {len(mismatches)}")
    for m in mismatches:
        print(f"  {m}")


if __name__ == "__main__":
    main()
