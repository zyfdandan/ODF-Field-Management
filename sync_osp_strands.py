#!/usr/bin/env python3
"""Sync netbox-osp Strand/Splice data from FMS/DCIM trunk cables and PATCH/LINK jumps."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from import_from_excel import NetBoxClient

DEFAULT_CONFIG = Path(__file__).with_name("netbox_config.json")
TRUNK_PREFIX = "CBL-"
JUMP_PREFIXES = ("PATCH-", "LINK-")


def paginate(client: NetBoxClient, path: str, params: dict | None = None) -> list[dict[str, Any]]:
    params = dict(params or {})
    params.setdefault("limit", 200)
    out: list[dict[str, Any]] = []
    while True:
        data = client.request("GET", path, params=params)
        out.extend(data.get("results", []))
        if not data.get("next"):
            break
        params["offset"] = params.get("offset", 0) + params["limit"]
    return out


def frontport_content_type_id(client: NetBoxClient) -> int:
    """NetBox 4.6+ uses /core/object-types/ (not django contenttypes id from POST probe)."""
    cached = getattr(client, "_osp_frontport_ct", None)
    if cached:
        return cached
    for ot in paginate(client, "/core/object-types/"):
        if ot.get("app_label") == "dcim" and ot.get("model") == "frontport":
            client._osp_frontport_ct = int(ot["id"])
            return client._osp_frontport_ct
    raise RuntimeError("找不到 dcim.frontport 的 object-type，无法同步 OSP Strand")


def strand_claims_port(strand: dict[str, Any], fp_id: int) -> bool:
    return strand.get("a_termination_id") == fp_id or strand.get("b_termination_id") == fp_id


def find_strand_for_port(strands: list[dict[str, Any]], fp_id: int) -> dict[str, Any] | None:
    for strand in strands:
        if strand_claims_port(strand, fp_id):
            return strand
    return None


class OspSync:
    def __init__(self, client: NetBoxClient) -> None:
        self.client = client
        self.fp_ct = frontport_content_type_id(client)
        self._closures: dict[str, int] = {}
        self._trays: dict[str, int] = {}
        self._osp_cables: dict[str, int] = {}
        self._strands: list[dict[str, Any]] = []

    def repair_strand_termination_types(self) -> int:
        """Fix strands created with wrong a/b_termination_type (NetBox 4.6 object-type id)."""
        fixed = 0
        for strand in paginate(self.client, "/plugins/osp/strands/"):
            needs = (
                strand.get("a_termination_id") and strand.get("a_termination_type") != self.fp_ct
            ) or (
                strand.get("b_termination_id") and strand.get("b_termination_type") != self.fp_ct
            )
            if not needs:
                continue
            payload: dict[str, Any] = {}
            if strand.get("a_termination_id"):
                payload["a_termination_type"] = self.fp_ct
                payload["a_termination_id"] = strand["a_termination_id"]
            if strand.get("b_termination_id"):
                payload["b_termination_type"] = self.fp_ct
                payload["b_termination_id"] = strand["b_termination_id"]
            self.client.request("PATCH", f"/plugins/osp/strands/{strand['id']}/", json=payload)
            fixed += 1
        if fixed:
            print(f"  修正 Strand termination_type: {fixed} 条 -> {self.fp_ct}")
        return fixed

    def reload_strands(self) -> None:
        self._strands = paginate(self.client, "/plugins/osp/strands/")

    def ensure_osp_cable(self, dcim_cable: dict[str, Any]) -> int:
        label = (dcim_cable.get("label") or "").strip()
        if label in self._osp_cables:
            return self._osp_cables[label]

        hits = self.client.request("GET", "/plugins/osp/cables/", params={"cid": label, "limit": 1})
        if hits.get("results"):
            cid = hits["results"][0]["id"]
            self._osp_cables[label] = cid
            return cid

        a_dev = dcim_cable["a_terminations"][0]["object"]["device"]
        b_dev = dcim_cable["b_terminations"][0]["object"]["device"]
        dev_a = self.client.request("GET", f"/dcim/devices/{a_dev['id']}/")
        dev_b = self.client.request("GET", f"/dcim/devices/{b_dev['id']}/")
        length = dcim_cable.get("length") or 100
        created = self.client.request(
            "POST",
            "/plugins/osp/cables/",
            json={
                "cid": label,
                "type": "loose-tube-armoured",
                "status": "active",
                "fibre_count": 12,
                "tube_count": 1,
                "fibres_per_tube": 12,
                "length_m": int(float(length)) if length else 100,
                "attenuation_db_per_km": "0.22",
                "site_a": dev_a["site"]["id"],
                "site_b": dev_b["site"]["id"],
                "description": f"Synced from DCIM {label}",
            },
        )
        self._osp_cables[label] = created["id"]
        print(f"  OSP 光缆: {label}")
        return created["id"]

    def rear_front_map(self, rear_port_id: int) -> dict[int, int]:
        rp = self.client.request("GET", f"/dcim/rear-ports/{rear_port_id}/")
        return {link["position"]: link["front_port"] for link in rp.get("front_ports", [])}

    def fp_exists(self, fp_id: int) -> bool:
        try:
            self.client.request("GET", f"/dcim/front-ports/{fp_id}/")
            return True
        except RuntimeError:
            return False

    def upsert_strand(
        self,
        osp_cable_id: int,
        position: int,
        fp_a: int,
        fp_b: int,
        dcim_cable_id: int,
        in_use: bool,
    ) -> dict[str, Any]:
        status = "in-use" if in_use else "spare"
        existing = next(
            (
                s
                for s in self._strands
                if s.get("cable") == osp_cable_id and s.get("position") == position
            ),
            None,
        )
        payload = {
            "cable": osp_cable_id,
            "position": position,
            "status": status,
            "cable_link": dcim_cable_id,
            "a_termination_type": self.fp_ct,
            "a_termination_id": fp_a,
            "b_termination_type": self.fp_ct,
            "b_termination_id": fp_b,
        }
        if existing:
            need_patch = any(
                existing.get(k) != payload.get(k)
                for k in (
                    "status",
                    "cable_link",
                    "a_termination_type",
                    "a_termination_id",
                    "b_termination_type",
                    "b_termination_id",
                )
            )
            if need_patch:
                updated = self.client.request(
                    "PATCH",
                    f"/plugins/osp/strands/{existing['id']}/",
                    json=payload,
                )
                print(f"  更新 Strand: {updated.get('display', existing['id'])}")
                return updated
            return existing

        created = self.client.request("POST", "/plugins/osp/strands/", json=payload)
        print(f"  新建 Strand: {created.get('display', created['id'])}")
        self._strands.append(created)
        return created

    def sync_trunk_cable(self, dcim_cable: dict[str, Any], used_fp_ids: set[int]) -> int:
        label = dcim_cable.get("label") or ""
        osp_cable_id = self.ensure_osp_cable(dcim_cable)
        a_rp = dcim_cable["a_terminations"][0]["object"]["id"]
        b_rp = dcim_cable["b_terminations"][0]["object"]["id"]
        map_a = self.rear_front_map(a_rp)
        map_b = self.rear_front_map(b_rp)
        positions = sorted(set(map_a) & set(map_b))
        synced = 0
        for pos in positions:
            fp_a = map_a[pos]
            fp_b = map_b[pos]
            if not self.fp_exists(fp_a) or not self.fp_exists(fp_b):
                continue
            in_use = fp_a in used_fp_ids or fp_b in used_fp_ids
            self.upsert_strand(osp_cable_id, pos, fp_a, fp_b, dcim_cable["id"], in_use)
            synced += 1
        return synced

    def ensure_closure(self, dev_name: str, site_id: int) -> int:
        if dev_name in self._closures:
            return self._closures[dev_name]
        name = f"{dev_name}-OSP"
        hits = self.client.request("GET", "/plugins/osp/closures/", params={"name": name, "limit": 1})
        if hits.get("results"):
            cid = hits["results"][0]["id"]
        else:
            cid = self.client.request(
                "POST",
                "/plugins/osp/closures/",
                json={"name": name, "site": site_id, "status": "active", "description": "Excel/FMS 同步"},
            )["id"]
            print(f"  OSP 熔接盒: {name}")
        self._closures[dev_name] = cid
        return cid

    def ensure_tray(self, closure_id: int, dev_name: str) -> int:
        if dev_name in self._trays:
            return self._trays[dev_name]
        hits = self.client.request("GET", "/plugins/osp/trays/", params={"closure_id": closure_id, "limit": 5})
        if hits.get("results"):
            tid = hits["results"][0]["id"]
        else:
            tid = self.client.request(
                "POST",
                "/plugins/osp/trays/",
                json={"closure": closure_id, "number": 1},
            )["id"]
        self._trays[dev_name] = tid
        return tid

    def ensure_splice(self, tray_id: int, strand_a: int, strand_b: int, note: str) -> None:
        if strand_a == strand_b:
            return
        pair = {strand_a, strand_b}
        for sp in paginate(self.client, "/plugins/osp/splices/"):
            if {sp.get("strand_a"), sp.get("strand_b")} == pair:
                return
        pos = sum(1 for _ in paginate(self.client, "/plugins/osp/splices/", {"tray_id": tray_id})) + 1
        self.client.request(
            "POST",
            "/plugins/osp/splices/",
            json={
                "tray": tray_id,
                "position": pos,
                "splice_type": "fusion",
                "strand_a": strand_a,
                "strand_b": strand_b,
                "loss_db": "0.08",
                "description": note[:200],
            },
        )
        print(f"  OSP 熔接: strand {strand_a} <-> {strand_b}")

    def sync_jump_cable(self, dcim_cable: dict[str, Any]) -> None:
        terms = (dcim_cable.get("a_terminations") or []) + (dcim_cable.get("b_terminations") or [])
        fps = [t["object"] for t in terms if t.get("object_type") == "dcim.frontport"]
        if len(fps) != 2:
            return
        sa = find_strand_for_port(self._strands, fps[0]["id"])
        sb = find_strand_for_port(self._strands, fps[1]["id"])
        if not sa or not sb:
            print(
                f"  跳过跳线 {dcim_cable.get('label')}: "
                f"端口 {fps[0]['name']} / {fps[1]['name']} 未找到 OSP Strand"
            )
            return
        dev_name = fps[0]["device"]["name"]
        if fps[1]["device"]["name"] != dev_name:
            dev_name = f"{fps[0]['device']['name']}-X-{fps[1]['device']['name']}"
        dev = self.client.request("GET", f"/dcim/devices/{fps[0]['device']['id']}/")
        closure_id = self.ensure_closure(dev_name, dev["site"]["id"])
        tray_id = self.ensure_tray(closure_id, dev_name)
        note = f"{fps[0]['name']} <-> {fps[1]['name']} ({dcim_cable.get('label', '')})"
        self.ensure_splice(tray_id, sa["id"], sb["id"], note)


def collect_used_front_ports(client: NetBoxClient) -> set[int]:
    used: set[int] = set()
    for path in paginate(client, "/plugins/fms/fiber-circuit-paths/"):
        for key in ("origin", "destination"):
            obj = path.get(key)
            if isinstance(obj, dict) and obj.get("id"):
                used.add(int(obj["id"]))
            elif obj:
                used.add(int(obj))
    return used


def sync_osp_from_dcim(client: NetBoxClient) -> dict[str, int]:
    sync = OspSync(client)
    stats = {"trunks": 0, "strands": 0, "jumps": 0, "splices": 0, "repaired": 0}
    stats["repaired"] = sync.repair_strand_termination_types()
    sync.reload_strands()
    used_fp_ids = collect_used_front_ports(client)

    dcim_cables = paginate(client, "/dcim/cables/")
    trunks = [c for c in dcim_cables if (c.get("label") or "").startswith(TRUNK_PREFIX)]
    jumps = [c for c in dcim_cables if (c.get("label") or "").startswith(JUMP_PREFIXES)]

    print(f"==> 同步 OSP 主干缆段 ({len(trunks)} 条)")
    for cab in trunks:
        terms = (cab.get("a_terminations") or []) + (cab.get("b_terminations") or [])
        if len(terms) != 2:
            continue
        if any(t.get("object_type") != "dcim.rearport" for t in terms):
            continue
        n = sync.sync_trunk_cable(cab, used_fp_ids)
        stats["trunks"] += 1
        stats["strands"] += n

    sync.reload_strands()
    print(f"\n==> 同步 OSP 跳纤熔接 ({len(jumps)} 条 PATCH/LINK)")
    before = len(paginate(client, "/plugins/osp/splices/"))
    for cab in jumps:
        sync.sync_jump_cable(cab)
        stats["jumps"] += 1
    stats["splices"] = len(paginate(client, "/plugins/osp/splices/")) - before
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync netbox-osp strands/splices from DCIM/FMS topology")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    if not args.config.exists():
        print(f"缺少配置: {args.config}", file=sys.stderr)
        sys.exit(1)

    config = json.loads(args.config.read_text(encoding="utf-8"))
    client = NetBoxClient(config["base_url"], config["token"], config.get("verify_ssl", True))
    stats = sync_osp_from_dcim(client)
    print(
        f"\n完成: 修正 {stats['repaired']} 条, {stats['trunks']} 主干缆, {stats['strands']} Strand 行, "
        f"{stats['jumps']} 跳线, 新增 {stats['splices']} 熔接"
    )


if __name__ == "__main__":
    main()
