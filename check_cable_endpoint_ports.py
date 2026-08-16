#!/usr/bin/env python3
"""Per-trunk: compare front/rear counts on each ODF for the same DCIM cable."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from import_asset_infra import DEFAULT_XLSX, parse_workbook
from import_from_excel import NetBoxClient
from naming_rules import mirror_cable_label
from reset_fms_data import paginate

OUT = ROOT / "cable_endpoint_port_report.txt"


def rp_stats(client: NetBoxClient, device_id: int, rp_name: str) -> dict:
    hits = client.request("GET", "/dcim/rear-ports/", params={"device_id": device_id, "name": rp_name, "limit": 1})
    if not hits.get("results"):
        return {"found": False, "positions": 0, "front": 0, "name": rp_name}
    rp = client.request("GET", f"/dcim/rear-ports/{hits['results'][0]['id']}/")
    return {
        "found": True,
        "id": rp["id"],
        "name": rp_name,
        "positions": int(rp.get("positions") or 0),
        "front": len(rp.get("front_ports") or []),
        "on_cable": bool(rp.get("cable")),
    }


def main() -> None:
    cfg = json.loads((ROOT / "netbox_config.json").read_text(encoding="utf-8"))
    client = NetBoxClient(cfg["base_url"], cfg["token"], False)
    sheet = parse_workbook(DEFAULT_XLSX)

    lines: list[str] = []
    issues: list[str] = []

    for row in sheet["trunks"]:
        label = row["label"]
        dev_a, dev_b = row["dev_a"], row["dev_b"]
        expected = int(row.get("strands") or 12)
        mirror = mirror_cable_label(label)

        hits = client.request("GET", "/dcim/cables/", params={"label": label, "limit": 1})
        if not hits.get("results"):
            continue

        def dev_id(name: str) -> int:
            h = client.request("GET", "/dcim/devices/", params={"name": name, "limit": 1})
            return h["results"][0]["id"]

        id_a, id_b = dev_id(dev_a), dev_id(dev_b)

        # rear ports that matter for this cable
        names_a = [label, mirror, dev_a]
        names_b = [label, mirror, dev_b]
        stats_a = {n: rp_stats(client, id_a, n) for n in dict.fromkeys(names_a)}
        stats_b = {n: rp_stats(client, id_b, n) for n in dict.fromkeys(names_b)}

        cable_rp_a = stats_a.get(label) or stats_a.get(dev_a)
        cable_rp_b = stats_b.get(label) or stats_b.get(dev_b)
        local_a = stats_a.get(dev_a)
        local_b = stats_b.get(dev_b)

        problem = False
        reasons: list[str] = []

        if cable_rp_a and cable_rp_b and cable_rp_a["found"] and cable_rp_b["found"]:
            if cable_rp_a["front"] != cable_rp_b["front"]:
                problem = True
                reasons.append(
                    f"缆段rear {label!r} front: {dev_a}={cable_rp_a['front']} vs {dev_b}={cable_rp_b['front']}"
                )
            if cable_rp_a["positions"] != cable_rp_b["positions"]:
                problem = True
                reasons.append(
                    f"缆段rear positions: {cable_rp_a['positions']} vs {cable_rp_b['positions']}"
                )

        if local_a and local_a["found"] and local_b and local_b["found"] and local_a["name"] != local_b["name"]:
            if local_a["front"] != local_b["front"]:
                problem = True
                reasons.append(
                    f"本端rear {local_a['name']!r}/{local_b['name']!r} front: {local_a['front']} vs {local_b['front']}"
                )

        # UI route by local device name cable (mirror) shows wrong count
        if local_b and local_b["found"] and local_b["front"] == 0 and cable_rp_b and cable_rp_b["front"] > 0:
            problem = True
            reasons.append(
                f"{dev_b} 本端rear {local_b['name']!r} 无front口，端口在 {cable_rp_b['name']!r}({cable_rp_b['front']})"
            )
        if local_a and local_a["found"] and local_a["front"] == 0 and cable_rp_a and cable_rp_a["front"] > 0:
            problem = True
            reasons.append(
                f"{dev_a} 本端rear {local_a['name']!r} 无front口，端口在 {cable_rp_a['name']!r}({cable_rp_a['front']})"
            )

        if problem:
            issues.append(label)
            lines.append(f"=== {label} (表{expected}芯) ===")
            for r in reasons:
                lines.append(f"  · {r}")
            for dev, did, stats in ((dev_a, id_a, stats_a), (dev_b, id_b, stats_b)):
                lines.append(f"  [{dev}]")
                for n, st in stats.items():
                    if st["found"]:
                        lines.append(
                            f"    {n!r}: pos={st['positions']} front={st['front']} cable={st['on_cable']}"
                        )
            lines.append("")

    header = f"同缆两端端口不一致: {len(issues)} / {len(sheet['trunks'])}\n"
    OUT.write_text(header + "\n".join(lines), encoding="utf-8")
    print(header.strip())
    for x in issues:
        print(f"  {x}")


if __name__ == "__main__":
    main()
