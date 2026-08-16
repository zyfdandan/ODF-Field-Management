#!/usr/bin/env python3
"""Quick verify FMS strand counts vs rear-port positions vs front ports."""

import json
from pathlib import Path

from import_asset_infra import DEFAULT_XLSX, parse_workbook
from import_from_excel import NetBoxClient
from reset_fms_data import paginate


def _front_port_count(client: NetBoxClient, device_id: int, rp_name: str) -> tuple[int, int]:
    hits = client.request("GET", "/dcim/rear-ports/", params={"device_id": device_id, "name": rp_name, "limit": 1})
    if not hits.get("results"):
        return 0, 0
    rp = client.request("GET", f"/dcim/rear-ports/{hits['results'][0]['id']}/")
    return int(rp.get("positions") or 0), len(rp.get("front_ports") or [])


def main() -> None:
    cfg = json.loads(Path("netbox_config.json").read_text(encoding="utf-8"))
    client = NetBoxClient(cfg["base_url"], cfg["token"], cfg.get("verify_ssl", False))
    data = parse_workbook(DEFAULT_XLSX)

    mismatches = []
    for row in data["trunks"]:
        label = row["label"]
        expected = int(row.get("strands") or 12)
        hits = client.request("GET", "/dcim/cables/", params={"label": label, "limit": 1})
        if not hits.get("results"):
            mismatches.append((label, expected, "no cable"))
            continue
        cable = hits["results"][0]
        fc_hits = client.request("GET", "/plugins/fms/fiber-cables/", params={"cable_id": cable["id"], "limit": 1})
        if not fc_hits.get("results"):
            mismatches.append((label, expected, "no fms cable"))
            continue
        fc_id = fc_hits["results"][0]["id"]
        strands = len(paginate(client, "/plugins/fms/fiber-strands/", {"fiber_cable_id": fc_id}))
        dev_ids = set()
        for side in ("a_terminations", "b_terminations"):
            obj = cable[side][0]["object"]
            dev_ids.add((obj["device"]["name"], obj["device"]["id"]))
        issues = []
        if strands != expected:
            issues.append(f"strands={strands}")
        for _dev_name, dev_id in dev_ids:
            positions, front = _front_port_count(client, dev_id, label)
            if positions != expected:
                issues.append(f"rear@{dev_id}={positions}")
            if front != expected:
                issues.append(f"front@{dev_id}={front}")
        if issues:
            mismatches.append((label, expected, " ".join(issues)))

    print(f"Checked {len(data['trunks'])} trunks, mismatches: {len(mismatches)}")
    for item in mismatches[:20]:
        print(f"  {item[0]}: want {item[1]} -> {item[2]}")
    if len(mismatches) > 20:
        print(f"  ... and {len(mismatches) - 20} more")


if __name__ == "__main__":
    main()
