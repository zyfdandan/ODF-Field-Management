#!/usr/bin/env python3
"""Find cable endpoints where ODF strand counts differ between A/B sides."""

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

OUT = ROOT / "strand_asymmetry_report.txt"


def device_strand_capacity(client: NetBoxClient, device: dict) -> int | None:
    dt = device.get("device_type") or {}
    model = (dt.get("model") or "").strip()
    if model.startswith("ODF-") and model[4:].isdigit():
        return int(model[4:])
    return None


def rear_port_stats(client: NetBoxClient, device_id: int, rp_name: str) -> dict:
    hits = client.request("GET", "/dcim/rear-ports/", params={"device_id": device_id, "name": rp_name, "limit": 1})
    if not hits.get("results"):
        return {"positions": 0, "front": 0, "found": False}
    rp = client.request("GET", f"/dcim/rear-ports/{hits['results'][0]['id']}/")
    return {
        "found": True,
        "positions": int(rp.get("positions") or 0),
        "front": len(rp.get("front_ports") or []),
        "id": rp["id"],
    }


def fms_strand_count(client: NetBoxClient, cable_id: int) -> int:
    fc = client.request("GET", "/plugins/fms/fiber-cables/", params={"cable_id": cable_id, "limit": 1})
    if not fc.get("results"):
        return 0
    fc_id = fc["results"][0]["id"]
    return len(paginate(client, "/plugins/fms/fiber-strands/", {"fiber_cable_id": fc_id}))


def cable_endpoints(client: NetBoxClient, cable: dict) -> list[dict]:
    out: list[dict] = []
    for side in ("a_terminations", "b_terminations"):
        for term in cable.get(side) or []:
            obj = term.get("object") or {}
            dev = obj.get("device") or {}
            if not dev.get("id"):
                continue
            dev_full = client.request("GET", f"/dcim/devices/{dev['id']}/")
            rp_name = (obj.get("name") or "").strip()
            out.append(
                {
                    "side": side,
                    "device": dev_full.get("name") or "",
                    "device_id": dev_full["id"],
                    "rp_name": rp_name,
                    "odf_model": (dev_full.get("device_type") or {}).get("model", ""),
                    "odf_capacity": device_strand_capacity(client, dev_full),
                    "rear": rear_port_stats(client, dev_full["id"], rp_name),
                }
            )
    return out


def main() -> None:
    cfg = json.loads((ROOT / "netbox_config.json").read_text(encoding="utf-8"))
    client = NetBoxClient(cfg["base_url"], cfg["token"], cfg.get("verify_ssl", False))
    sheet = parse_workbook(DEFAULT_XLSX)
    expected_by_label = {t["label"]: int(t.get("strands") or 12) for t in sheet["trunks"]}

    lines: list[str] = []
    asym: list[tuple[str, dict]] = []

    for row in sheet["trunks"]:
        label = row["label"]
        expected = expected_by_label[label]
        hits = client.request("GET", "/dcim/cables/", params={"label": label, "limit": 1})
        if not hits.get("results"):
            continue
        cable = hits["results"][0]
        fms = fms_strand_count(client, cable["id"])
        eps = cable_endpoints(client, cable)
        if len(eps) != 2:
            continue

        caps = [e["odf_capacity"] for e in eps]
        rears = [e["rear"]["positions"] for e in eps if e["rear"]["found"]]
        fronts = [e["rear"]["front"] for e in eps if e["rear"]["found"]]

        mismatch = False
        reasons: list[str] = []
        if len(set(c for c in caps if c)) > 1:
            mismatch = True
            reasons.append(f"ODF型号 {eps[0]['device']}={caps[0]} vs {eps[1]['device']}={caps[1]}")
        if len(set(rears)) > 1:
            mismatch = True
            reasons.append(f"rear positions {rears[0]} vs {rears[1]} (rp={eps[0]['rp_name']!r}/{eps[1]['rp_name']!r})")
        if len(set(fronts)) > 1:
            mismatch = True
            reasons.append(f"front ports {fronts[0]} vs {fronts[1]}")
        if fms and fms != expected:
            mismatch = True
            reasons.append(f"FMS={fms} expected={expected}")

        if mismatch:
            asym.append((label, {"expected": expected, "fms": fms, "endpoints": eps, "reasons": reasons}))

    lines.append(f"检查缆段 {len(sheet['trunks'])} 条，两端芯数不一致 {len(asym)} 条\n")
    for label, info in asym:
        lines.append(f"=== {label} (确认表 {info['expected']}芯, FMS {info['fms']}芯) ===")
        for r in info["reasons"]:
            lines.append(f"  · {r}")
        for e in info["endpoints"]:
            lines.append(
                f"  {e['device']}: model={e['odf_model']} rear={e['rear']['positions']} front={e['rear']['front']} rp={e['rp_name']!r}"
            )
        lines.append("")

    # highlight user case
    for kw in ("检化验", "检验", "质检", "轧钢"):
        hits = [x for x in asym if kw in x[0]]
        if hits:
            lines.append(f"\n--- 含「{kw}」的不一致 ---")
            for label, _ in hits:
                lines.append(f"  {label}")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT}")
    print(f"Asymmetric: {len(asym)}")
    for label, info in asym[:25]:
        print(f"  {label}: {'; '.join(info['reasons'])}")


if __name__ == "__main__":
    main()
