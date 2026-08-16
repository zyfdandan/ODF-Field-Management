#!/usr/bin/env python3
"""NetBox FMS infrastructure: multi-cable ports, splices, cable terminations."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Protocol

from naming_rules import (
    cable_belongs_on_route_odf,
    cable_label_variants,
    cable_port_prefix,
    fiber_from_position,
    legacy_diagonal_fiber,
    local_room_from_odf_name,
    mirror_cable_label,
    normalize_fiber_label,
    port_name_for_cable_fiber,
    position_from_fiber,
)


class ApiClient(Protocol):
    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]: ...


def _parse_dependent_cable_id(error_text: str) -> int | None:
    """Extract dependent cable id from NetBox 409 delete response."""
    m = re.search(r"cable:.*?\((\d+)\)", error_text)
    return int(m.group(1)) if m else None


def _jump_cable_sort_key(label: str) -> tuple[int, str]:
    """Delete cross-ODF LINK cables before same-ODF PATCH cables."""
    if label.startswith("LINK-"):
        return (0, label)
    if label.startswith("PATCH-"):
        return (1, label)
    return (2, label)


def _short_odf_name(full_name: str) -> str:
    parts = full_name.replace("机房", " ").split("-")
    if len(parts) >= 4:
        return f"{parts[0].strip()}{parts[1]}-{parts[-1]}"
    return full_name[:18]


def build_fiber_chain_from_trace(trace: dict[str, Any]) -> tuple[str, str]:
    """Build simple port chain and per-ODF detail from trace API JSON."""
    simple: list[str] = []
    detail_lines: list[str] = []

    def append_port(name: str) -> None:
        if not simple or simple[-1] != name:
            simple.append(name)

    hops = list(trace.get("hops") or [])
    device_indices = [i for i, h in enumerate(hops) if h.get("type") == "device"]

    for i, hop in enumerate(hops):
        hop_type = hop.get("type")
        if hop_type == "port_link":
            pa = hop.get("port_a") or {}
            pb = hop.get("port_b") or {}
            da = _short_odf_name((pa.get("device") or {}).get("name", ""))
            db = _short_odf_name((pb.get("device") or {}).get("name", ""))
            na, nb = pa.get("name", ""), pb.get("name", "")
            if na and nb:
                link_text = f"{da}:{na} ↔ {db}:{nb}"
                detail_lines.append(f"{hop.get('link_kind', '跳纤')}: {link_text}")
                append_port(na)
                append_port(nb)
            continue
        if hop_type == "cable":
            label = (hop.get("label") or "").strip()
            if label:
                detail_lines.append(f"缆段: {label}")
            continue
        if hop_type != "device":
            continue
        dev = _short_odf_name(hop.get("name", ""))
        is_first = bool(device_indices) and i == device_indices[0]
        is_last = bool(device_indices) and i == device_indices[-1]
        role = "起" if is_first else ("终" if is_last else "经")

        ports = hop.get("ports") or {}
        if ports.get("front_port"):
            fp = ports["front_port"]["name"]
            append_port(fp)
            detail_lines.append(f"{dev}: {fp}（{role}）")
            continue

        ingress = (hop.get("ingress") or {}).get("front_port") or {}
        egress = (hop.get("egress") or {}).get("front_port") or {}
        ing = ingress.get("name")
        egr = egress.get("name")
        if ing and egr:
            if ing != egr:
                detail_lines.append(f"{dev}: {ing} → {egr}（熔接）")
                append_port(ing)
                append_port(egr)
            else:
                detail_lines.append(f"{dev}: {ing}（{role}）")
                append_port(ing)
        elif ing:
            detail_lines.append(f"{dev}: {ing}（{role}）")
            append_port(ing)
        elif egr:
            detail_lines.append(f"{dev}: {egr}（{role}）")
            append_port(egr)

    return " → ".join(simple), "\n".join(detail_lines)

def infer_links_from_segments(groups: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    """When adjacent segments meet at same ODF/device, add port link across cables (incl. cross-fiber)."""
    inferred: list[dict[str, str]] = []
    for cid, segs in groups.items():
        for i in range(len(segs) - 1):
            s1, s2 = segs[i], segs[i + 1]
            if s1["dev_b"] != s2["dev_a"]:
                continue
            cab_a, cab_b = (s1.get("cable") or "").strip(), (s2.get("cable") or "").strip()
            if not cab_a or not cab_b or cab_a == cab_b:
                continue
            if s1["port_b"] == s2["port_a"] and s1["dev_b"] == s2["dev_a"]:
                continue
            if s1["port_b"] == s2["port_a"]:
                note = f"自动段间 {cid} 段{i + 1}→{i + 2}"
            else:
                note = f"自动熔接 {cid} 段{i + 1}→{i + 2} ({s1['port_b']}→{s2['port_a']})"
            inferred.append(
                {
                    "dev_a": s1["dev_b"],
                    "port_a": s1["port_b"],
                    "cab_a": cab_a,
                    "dev_b": s2["dev_a"],
                    "port_b": s2["port_a"],
                    "cab_b": cab_b,
                    "note": note,
                    "cid": cid,
                }
            )
    return inferred


def cross_fiber_splices_from_segments(segments: list[dict[str, str]]) -> list[dict[str, str]]:
    """Trunk row with port_a != port_b: FMS splice on B-side ODF (cable landing -> registered port)."""
    splices: list[dict[str, str]] = []
    for seg in segments:
        pa = (seg.get("port_a") or "").strip()
        pb = (seg.get("port_b") or "").strip()
        if not pa or not pb or pa == pb:
            continue
        cab = (seg.get("cable") or "").strip()
        da = (seg.get("dev_a") or "").strip()
        db = (seg.get("dev_b") or "").strip()
        if not cab or not da or not db:
            continue
        if not cable_belongs_on_route_odf(da, cab):
            continue
        splices.append(
            {
                "dev": db,
                "port_a": pa,
                "cab_a": cab,
                "port_b": pb,
                "cab_b": cab,
                "note": f"异纤 B端 {da}->{db} {pa}->{pb}",
                "cid": seg.get("cid", ""),
            }
        )
    return splices


def sort_links_for_apply(links: list[dict[str, str]]) -> list[dict[str, str]]:
    """Apply same-ODF PATCH (incl. cross-fiber) before cross-ODF LINK so jump legs are not cleared."""

    def _prio(lk: dict[str, str]) -> tuple[int, str]:
        da = (lk.get("dev_a") or "").strip()
        db = (lk.get("dev_b") or "").strip()
        if da and da == db:
            return (0, lk.get("note") or "")
        return (1, lk.get("note") or "")

    return sorted(links, key=_prio)


def jump_links_from_segments(segments: list[dict[str, str]]) -> list[dict[str, str]]:
    """Cross-ODF jump rows in circuit store → port link for apply_port_links."""
    from naming_rules import cable_belongs_on_route_odf

    links: list[dict[str, str]] = []
    for seg in segments:
        da = (seg.get("dev_a") or "").strip()
        db = (seg.get("dev_b") or "").strip()
        if not da or not db or da == db:
            continue
        cab = (seg.get("cable") or "").strip()
        cab_a = (seg.get("cab_a") or cab).strip()
        cab_b = (seg.get("cab_b") or cab).strip()
        is_jump = "(跳纤段)" in (seg.get("label_pos") or "") or not cable_belongs_on_route_odf(da, cab)
        if not is_jump:
            continue
        if not cable_belongs_on_route_odf(da, cab_a):
            cab_a = ""
        if not cable_belongs_on_route_odf(db, cab_b):
            cab_b = cab if cable_belongs_on_route_odf(db, cab) else cab_b
        links.append(
            {
                "dev_a": da,
                "port_a": seg.get("port_a", ""),
                "cab_a": cab_a,
                "dev_b": db,
                "port_b": seg.get("port_b", ""),
                "cab_b": cab_b,
                "note": seg.get("label_pos") or "跳纤段",
                "cid": seg.get("cid", ""),
            }
        )
    return links


def merge_link_rows(manual: list[dict[str, str]], inferred: list[dict[str, str]]) -> list[dict[str, str]]:
    def key(lk: dict[str, str]) -> tuple:
        return (
            lk["dev_a"],
            lk.get("cab_a", ""),
            lk["port_a"],
            lk["dev_b"],
            lk.get("cab_b", ""),
            lk["port_b"],
        )

    merged: dict[tuple, dict[str, str]] = {key(lk): lk for lk in inferred}
    for lk in manual:
        merged[key(lk)] = lk
    return list(merged.values())


def splice_rows_to_links(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Convert legacy 熔接录入 rows to 端口连接 format (same ODF both sides)."""
    links: list[dict[str, str]] = []
    for sp in rows:
        dev = sp["dev"]
        links.append(
            {
                "dev_a": dev,
                "port_a": sp["port_a"],
                "cab_a": sp.get("cab_a", ""),
                "dev_b": dev,
                "port_b": sp["port_b"],
                "cab_b": sp.get("cab_b", ""),
                "note": sp.get("note", ""),
                "cid": sp.get("cid", ""),
            }
        )
    return links


def infer_splices_from_segments(groups: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    """When adjacent Excel segments meet at same ODF+fiber but different cables, add splice."""
    inferred: list[dict[str, str]] = []
    for cid, segs in groups.items():
        for i in range(len(segs) - 1):
            s1, s2 = segs[i], segs[i + 1]
            if s1["dev_b"] != s2["dev_a"] or s1["port_b"] != s2["port_a"]:
                continue
            cab_a, cab_b = (s1.get("cable") or "").strip(), (s2.get("cable") or "").strip()
            if not cab_a or not cab_b or cab_a == cab_b:
                continue
            dev = s1["dev_b"]
            inferred.append(
                {
                    "dev": dev,
                    "plan": "",
                    "cab_a": cab_a,
                    "port_a": s1["port_b"],
                    "cab_b": cab_b,
                    "port_b": s2["port_a"],
                    "tray": "1",
                    "note": f"自动段间熔接 {cid} 段{i + 1}→{i + 2}",
                    "cid": cid,
                }
            )
    return inferred


def merge_splice_rows(manual: list[dict[str, str]], inferred: list[dict[str, str]]) -> list[dict[str, str]]:
    def key(sp: dict[str, str]) -> tuple:
        return (sp["dev"], sp.get("cab_a", ""), sp["port_a"], sp.get("cab_b", ""), sp["port_b"])

    merged: dict[tuple, dict[str, str]] = {key(sp): sp for sp in inferred}
    for sp in manual:
        merged[key(sp)] = sp
    return list(merged.values())


def collect_fiber_bindings(
    segments: list[dict[str, str]],
    splices: list[dict[str, str]] | None = None,
    links: list[dict[str, str]] | None = None,
) -> set[tuple[str, str, str]]:
    bindings: set[tuple[str, str, str]] = set()
    for seg in segments:
        cab = (seg.get("cable") or "").strip()
        cab_a = (seg.get("cab_a") or cab).strip()
        cab_b = (seg.get("cab_b") or cab).strip()
        # Jump/cross-cable rows carry different cables per end — never apply peer
        # cable rename onto the local ODF (e.g. 精轧至加热炉 @ 精轧至主控楼).
        if seg.get("dev_a") and cab_a and seg.get("port_a"):
            bindings.add((seg["dev_a"], cab_a, seg["port_a"]))
        if seg.get("dev_b") and cab_b and seg.get("port_b"):
            bindings.add((seg["dev_b"], cab_b, seg["port_b"]))
    for sp in splices or []:
        dev = sp["dev"]
        if sp.get("cab_a"):
            bindings.add((dev, sp["cab_a"], sp["port_a"]))
        if sp.get("cab_b"):
            bindings.add((dev, sp["cab_b"], sp["port_b"]))
    for lk in links or []:
        if lk.get("dev_a") and lk.get("cab_a"):
            bindings.add((lk["dev_a"], lk["cab_a"], lk["port_a"]))
        if lk.get("dev_b") and lk.get("cab_b"):
            bindings.add((lk["dev_b"], lk["cab_b"], lk["port_b"]))
    return bindings


def resolve_port_name(fiber: str, cable_label: str, *, local_room: str = "", device_name: str = "") -> str:
    """Map Excel 排-芯 + 缆段编号 to NetBox front-port name."""
    from naming_rules import local_room_from_odf_name, port_name_for_cable_fiber

    if not local_room and device_name:
        local_room = local_room_from_odf_name(device_name)
    return port_name_for_cable_fiber(cable_label, fiber, local_room=local_room)


def build_device_cables(
    segments: list[dict[str, str]],
    splices: list[dict[str, str]] | None = None,
    links: list[dict[str, str]] | None = None,
) -> dict[str, list[str]]:
    per_dev: dict[str, set[str]] = defaultdict(set)
    for seg in segments:
        cab = (seg.get("cable") or "").strip()
        cab_a = (seg.get("cab_a") or cab).strip()
        cab_b = (seg.get("cab_b") or cab).strip()
        if cab_a and cable_belongs_on_route_odf(seg["dev_a"], cab_a):
            per_dev[seg["dev_a"]].add(cab_a)
        if cab_b and cable_belongs_on_route_odf(seg["dev_b"], cab_b):
            per_dev[seg["dev_b"]].add(cab_b)
    for sp in splices or []:
        dev = sp["dev"]
        if sp.get("cab_a"):
            per_dev[dev].add(sp["cab_a"])
        if sp.get("cab_b"):
            per_dev[dev].add(sp["cab_b"])
    for lk in links or []:
        for dev, cab in ((lk.get("dev_a"), lk.get("cab_a")), (lk.get("dev_b"), lk.get("cab_b"))):
            if dev and cab:
                per_dev[dev].add(cab)
    return {dev: sorted(cabs) for dev, cabs in per_dev.items()}


class FiberInfra:
    def __init__(self, client: ApiClient) -> None:
        self.client = client
        self._dev: dict[str, int] = {}
        self._fc: dict[str, int] = {}
        self._fp: dict[tuple[int, str], int] = {}
        self._mod: dict[int, int] = {}

    def dev_id(self, name: str) -> int:
        name = str(name or "").strip()
        if not name or name.lower() in ("undefined", "null", "none"):
            raise RuntimeError("找不到设备: 名称为空，请确认对端 ODF 已解析")
        if name not in self._dev:
            hits = self.client.request("GET", "/dcim/devices/", params={"name": name, "limit": 1})
            if not hits.get("results"):
                raise RuntimeError(f"找不到设备: {name}")
            self._dev[name] = hits["results"][0]["id"]
        return self._dev[name]

    def fc_id(self, label: str) -> int:
        label = (label or "").strip()
        if not label:
            raise RuntimeError("缺少缆段标签")
        if label in self._fc:
            return self._fc[label]

        def _cache(found_label: str, fc_pk: int) -> int:
            for alias in cable_label_variants(label):
                self._fc[alias] = fc_pk
            self._fc[found_label] = fc_pk
            return fc_pk

        for variant in cable_label_variants(label):
            hits = self.client.request("GET", "/plugins/fms/fiber-cables/", params={"limit": 100})
            for fc in hits.get("results", []):
                cab = fc.get("cable") or {}
                if isinstance(cab, dict) and cab.get("label") == variant:
                    return _cache(variant, fc["id"])
            c = self.client.request("GET", "/dcim/cables/", params={"label": variant, "limit": 1})
            if not c.get("results"):
                continue
            fc = self.client.request(
                "GET", "/plugins/fms/fiber-cables/", params={"cable_id": c["results"][0]["id"], "limit": 1}
            )
            if not fc.get("results"):
                raise RuntimeError(f"找不到 FMS 光缆: {variant}（请确认 bootstrap_test_infra 已创建）")
            return _cache(variant, fc["results"][0]["id"])
        raise RuntimeError(f"找不到缆段: {label}")

    def fp_id(self, dev_name: str, fp_name: str) -> int:
        key = (self.dev_id(dev_name), fp_name)
        if key not in self._fp:
            hits = self.client.request(
                "GET", "/dcim/front-ports/", params={"device_id": key[0], "name": fp_name, "limit": 1}
            )
            if not hits.get("results"):
                raise RuntimeError(f"找不到端口 {dev_name}:{fp_name}")
            self._fp[key] = hits["results"][0]["id"]
        return self._fp[key]

    def fp_id_for_fiber(self, dev_name: str, fiber: str, cable_label: str) -> int:
        fiber = (fiber or "").strip()
        names: list[str] = []
        local_room = local_room_from_odf_name(dev_name)
        for f in (fiber, normalize_fiber_label(fiber), legacy_diagonal_fiber(fiber)):
            name = port_name_for_cable_fiber(cable_label, f, local_room=local_room)
            if name not in names:
                names.append(name)
        last_err = ""
        for name in names:
            try:
                return self.fp_id(dev_name, name)
            except RuntimeError as exc:
                last_err = str(exc)
        raise RuntimeError(last_err or f"找不到端口 {dev_name}:{names[0]}")

    def infer_cable_label(self, dev_a: str, dev_b: str) -> str | None:
        da, db = self.dev_id(dev_a), self.dev_id(dev_b)
        for cab in self.client.request("GET", "/dcim/cables/", params={"limit": 200}).get("results", []):
            terms = (cab.get("a_terminations") or []) + (cab.get("b_terminations") or [])
            dev_ids = set()
            for t in terms:
                obj = t.get("object") or {}
                dev = obj.get("device") or {}
                if isinstance(dev, dict) and dev.get("id"):
                    dev_ids.add(dev["id"])
            if {da, db} <= dev_ids or da in dev_ids and db in dev_ids:
                lbl = cab.get("label")
                if lbl:
                    return lbl
        return None

    def ensure_module(self, dev_name: str) -> int:
        did = self.dev_id(dev_name)
        if did in self._mod:
            return self._mod[did]
        bays = self.client.request("GET", "/dcim/module-bays/", params={"device_id": did, "limit": 5})
        if not bays.get("results"):
            self.client.request("POST", "/dcim/module-bays/", json={"device": did, "name": "Tray-1", "position": "1"})
            bays = self.client.request("GET", "/dcim/module-bays/", params={"device_id": did, "limit": 5})
        bay_id = bays["results"][0]["id"]
        mt = self.client.request("GET", "/dcim/module-types/", params={"model": "Tray-12", "limit": 1})
        if not mt.get("results"):
            mfg = self.client.request("GET", "/dcim/manufacturers/", params={"name": "Generic", "limit": 1})["results"][0]
            mt_id = self.client.request(
                "POST", "/dcim/module-types/", json={"manufacturer": mfg["id"], "model": "Tray-12"}
            )["id"]
        else:
            mt_id = mt["results"][0]["id"]
        mods = self.client.request("GET", "/dcim/modules/", params={"device_id": did, "limit": 5})
        if mods.get("results"):
            mid = mods["results"][0]["id"]
        else:
            mid = self.client.request(
                "POST",
                "/dcim/modules/",
                json={"device": did, "module_type": mt_id, "module_bay": bay_id, "status": "active"},
            )["id"]
        self._mod[did] = mid
        return mid

    def rename_strands(self, fc_id: int, cable_label: str) -> None:
        strands = self.client.request("GET", "/plugins/fms/fiber-strands/", params={"fiber_cable_id": fc_id, "limit": 50})
        for s in strands.get("results", []):
            pos = s["position"]
            fiber = fiber_from_position(pos)
            new_name = port_name_for_cable_fiber(cable_label, fiber)
            if s["name"] != new_name:
                self.client.request("PATCH", f"/plugins/fms/fiber-strands/{s['id']}/", json={"name": new_name})

    def _rear_port_detail(self, dev_name: str, rp_name: str) -> dict | None:
        did = self.dev_id(dev_name)
        hits = self.client.request("GET", "/dcim/rear-ports/", params={"device_id": did, "name": rp_name, "limit": 1})
        if not hits.get("results"):
            return None
        return self.client.request("GET", f"/dcim/rear-ports/{hits['results'][0]['id']}/")

    def _delete_rear_front_ports(self, dev_name: str, rp_name: str) -> None:
        rp = self._rear_port_detail(dev_name, rp_name)
        if not rp:
            return
        for link in rp.get("front_ports", []):
            fp_id = link.get("front_port")
            if not fp_id:
                continue
            try:
                self.client.request("DELETE", f"/dcim/front-ports/{fp_id}/")
            except RuntimeError:
                pass
            self._fp.pop((self.dev_id(dev_name), fp_id), None)

    def provision_cable(self, dev_name: str, cable_label: str) -> None:
        local_room = local_room_from_odf_name(dev_name)
        pfx = cable_port_prefix(cable_label, local_room)
        did = self.dev_id(dev_name)
        fc = self.fc_id(cable_label)
        rp = self._rear_port_detail(dev_name, cable_label)
        if rp:
            positions = int(rp.get("positions") or 0)
            front_count = len(rp.get("front_ports") or [])
            if positions and front_count >= positions:
                return
            if front_count:
                self._delete_rear_front_ports(dev_name, cable_label)
        else:
            fps = self.client.request("GET", "/dcim/front-ports/", params={"device_id": did, "limit": 200})
            if any(fp["name"].startswith(pfx) for fp in fps.get("results", [])):
                return
        self.rename_strands(fc, cable_label)
        try:
            self.client.request(
                "POST",
                "/plugins/fms/provision-ports/",
                json={"fiber_cable_id": fc, "device_id": did, "port_type": "splice"},
            )
            print(f"  配置端口: {dev_name} @ {cable_label} -> {pfx}*")
        except RuntimeError as e:
            if "duplicate" not in str(e).lower():
                raise

    def fix_cable_termination(self, cable_label: str) -> None:
        cable = None
        for variant in cable_label_variants(cable_label):
            hits = self.client.request("GET", "/dcim/cables/", params={"label": variant, "limit": 1})
            if hits.get("results"):
                cable = hits["results"][0]
                cable_label = variant
                break
        if not cable:
            return
        a_obj = cable["a_terminations"][0]["object"]
        b_obj = cable["b_terminations"][0]["object"]
        dev_a = a_obj["device"]["name"]
        dev_b = b_obj["device"]["name"]

        def _rp_for(dev_name: str) -> int:
            for name in (cable_label, mirror_cable_label(cable_label)):
                try:
                    return self.rp_id(dev_name, name)
                except RuntimeError:
                    continue
            raise RuntimeError(f"找不到后端口 {dev_name} @ {cable_label}")

        # FMS uses the DCIM cable label as rear-port name on both ODFs.
        try:
            rp_a = self.rp_id(dev_a, cable_label)
            rp_b = self.rp_id(dev_b, cable_label)
        except RuntimeError:
            rp_a = _rp_for(dev_a)
            rp_b = _rp_for(dev_b)
        if a_obj["id"] == rp_a and b_obj["id"] == rp_b:
            return
        self.client.request(
            "PATCH",
            f"/dcim/cables/{cable['id']}/",
            json={
                "a_terminations": [{"object_type": "dcim.rearport", "object_id": rp_a}],
                "b_terminations": [{"object_type": "dcim.rearport", "object_id": rp_b}],
            },
        )
        print(f"  缆线改接: {cable_label}")

    def rp_id(self, dev_name: str, rp_name: str) -> int:
        did = self.dev_id(dev_name)
        hits = self.client.request("GET", "/dcim/rear-ports/", params={"device_id": did, "name": rp_name, "limit": 1})
        if not hits.get("results"):
            raise RuntimeError(f"找不到后端口 {dev_name}:{rp_name}")
        return hits["results"][0]["id"]

    def rename_cable_front_ports(self, dev_name: str, cables: list[str]) -> None:
        did = self.dev_id(dev_name)
        local_room = local_room_from_odf_name(dev_name)
        for cab in cables:
            hits = self.client.request("GET", "/dcim/rear-ports/", params={"device_id": did, "name": cab, "limit": 1})
            if not hits.get("results"):
                continue
            rp = self.client.request("GET", f"/dcim/rear-ports/{hits['results'][0]['id']}/")
            for link in rp.get("front_ports", []):
                pos = link["position"]
                fp_id = link["front_port"]
                fp = self.client.request("GET", f"/dcim/front-ports/{fp_id}/")
                fiber = fiber_from_position(pos)
                new_name = port_name_for_cable_fiber(cab, fiber, local_room=local_room)
                if fp["name"] == new_name:
                    self._fp[(did, new_name)] = fp_id
                    continue
                clash = self.client.request(
                    "GET", "/dcim/front-ports/", params={"device_id": did, "name": new_name, "limit": 1}
                )
                if clash.get("results") and clash["results"][0]["id"] != fp_id:
                    continue
                self.client.request("PATCH", f"/dcim/front-ports/{fp_id}/", json={"name": new_name})
                self._fp[(did, new_name)] = fp_id
                if fp["name"] != new_name:
                    print(f"  端口: {dev_name} {fp['name']} -> {new_name}")

    def fix_device_port_names(self, dev_name: str) -> None:
        """Rename front ports from legacy N-N labels to 排-芯 (1-1..1-12, 2-1..)."""
        did = self.dev_id(dev_name)
        cables: list[str] = []
        for rp in self.client.request("GET", "/dcim/rear-ports/", params={"device_id": did, "limit": 200}).get(
            "results", []
        ):
            lbl = (rp.get("name") or "").strip()
            if lbl.upper().startswith("CBL-") or "至" in lbl:
                cables.append(lbl)
        if not cables:
            return
        self.rename_cable_front_ports(dev_name, sorted(set(cables)))
        for cab in cables:
            try:
                self.rename_strands(self.fc_id(cab), cab)
            except Exception:
                pass

    def rename_port_for_fiber(self, dev_name: str, cable_label: str, fiber: str) -> None:
        pos = position_from_fiber(fiber)
        did = self.dev_id(dev_name)
        # Same ODF may expose fronts on the mirrored rear name (A/B 端命名不一致).
        labels = cable_label_variants(cable_label) or [cable_label]
        fp_id = None
        chosen_label = cable_label
        for lbl in labels:
            hits = self.client.request(
                "GET", "/dcim/rear-ports/", params={"device_id": did, "name": lbl, "limit": 5}
            )
            # Prefer rears that actually have front mappings / attached cable.
            rears = list(hits.get("results") or [])
            rears.sort(
                key=lambda r: (
                    0 if r.get("cable") else 1,
                    -(len(r.get("front_ports") or []) if isinstance(r.get("front_ports"), list) else 0),
                )
            )
            for hit in rears:
                rp = self.client.request("GET", f"/dcim/rear-ports/{hit['id']}/")
                for link in rp.get("front_ports") or []:
                    if int(link.get("position") or 0) == pos:
                        fp_id = link.get("front_port")
                        chosen_label = lbl
                        break
                if fp_id:
                    break
            if fp_id:
                break
        if not fp_id:
            # Last resort: any front on this device whose rear_port_position matches.
            fronts = self.client.request(
                "GET", "/dcim/front-ports/", params={"device_id": did, "limit": 200}
            ).get("results", [])
            for fp in fronts:
                if int(fp.get("rear_port_position") or 0) == pos:
                    fp_id = fp.get("id")
                    break
        if not fp_id:
            raise RuntimeError(f"找不到 {dev_name}@{cable_label} position {pos} (fiber {fiber})")
        new_name = port_name_for_cable_fiber(
            chosen_label or cable_label, fiber, local_room=local_room_from_odf_name(dev_name)
        )
        fp = self.client.request("GET", f"/dcim/front-ports/{fp_id}/")
        if fp["name"] == new_name:
            self._fp[(did, new_name)] = fp_id
            return
        clash = self.client.request(
            "GET", "/dcim/front-ports/", params={"device_id": did, "name": new_name, "limit": 1}
        )
        if clash.get("results") and clash["results"][0]["id"] != fp_id:
            raise RuntimeError(f"端口名冲突 {dev_name}:{new_name}")
        self.client.request("PATCH", f"/dcim/front-ports/{fp_id}/", json={"name": new_name})
        self._fp[(did, new_name)] = fp_id
        print(f"  端口: {dev_name} {fp['name']} -> {new_name}")

    def apply_fiber_port_names(
        self,
        segments: list[dict[str, str]],
        splices: list[dict[str, str]] | None = None,
        links: list[dict[str, str]] | None = None,
    ) -> None:
        for dev, cab, fiber in sorted(collect_fiber_bindings(segments, splices, links)):
            if not cable_belongs_on_route_odf(dev, cab):
                continue
            self.rename_port_for_fiber(dev, cab, fiber)

    def ports_linked(self, dev_name: str, fp_a: str, fp_b: str) -> bool:
        a = self.fp_id(dev_name, fp_a)
        b = self.fp_id(dev_name, fp_b)
        fp = self.client.request("GET", f"/dcim/front-ports/{a}/")
        for peer in fp.get("link_peers") or []:
            if peer.get("id") == b:
                return True
        return False

    def delete_cable_resolving_deps(self, cab_id: int, _seen: set[int] | None = None) -> None:
        """Delete a jump cable, removing NetBox-dependent cables first (409-safe)."""
        seen = _seen if _seen is not None else set()
        if cab_id in seen:
            return
        seen.add(cab_id)
        try:
            self.client.request("DELETE", f"/dcim/cables/{cab_id}/")
        except RuntimeError as exc:
            msg = str(exc)
            if "409" not in msg:
                raise
            dep_id = _parse_dependent_cable_id(msg)
            if not dep_id or dep_id == cab_id:
                raise
            self.delete_cable_resolving_deps(dep_id, seen)
            self.client.request("DELETE", f"/dcim/cables/{cab_id}/")

    def collect_jump_cables_on_port(self, fp_id: int, keep_peer_id: int | None = None) -> list[tuple[int, str]]:
        fp = self.client.request("GET", f"/dcim/front-ports/{fp_id}/")
        found: dict[int, str] = {}
        peers = fp.get("link_peers") or []
        keep_cable_id: int | None = None
        if keep_peer_id:
            for peer in peers:
                if peer.get("id") == keep_peer_id:
                    cab = peer.get("cable") or fp.get("cable")
                    if isinstance(cab, dict) and cab.get("id"):
                        keep_cable_id = int(cab["id"])
                    break

        def maybe_add(cable: Any) -> None:
            if not isinstance(cable, dict) or not cable.get("id"):
                return
            cid = int(cable["id"])
            if cid == keep_cable_id:
                return
            label = (cable.get("label") or "").strip()
            if label.startswith(("PATCH-", "LINK-")):
                found[cid] = label

        maybe_add(fp.get("cable"))
        for peer in peers:
            if keep_peer_id and peer.get("id") == keep_peer_id:
                continue
            maybe_add(peer.get("cable"))
            maybe_add(fp.get("cable"))

        items = list(found.items())
        items.sort(key=lambda item: _jump_cable_sort_key(item[1]))
        return items

    def clear_jump_cables_on_port(self, dev_name: str, fp_name: str, keep_peer_id: int | None = None) -> int:
        removed = 0
        for _ in range(16):
            fp_id = self.fp_id(dev_name, fp_name)
            cables = self.collect_jump_cables_on_port(fp_id, keep_peer_id)
            if not cables:
                break
            for cab_id, label in cables:
                self.delete_cable_resolving_deps(cab_id)
                removed += 1
                print(f"  移除跳线: {dev_name} {fp_name} ({label})")
        return removed

    def _delete_jump_on_port(self, dev_name: str, fp_name: str, keep_peer_id: int | None = None) -> None:
        self.clear_jump_cables_on_port(dev_name, fp_name, keep_peer_id)

    def _delete_patch_on_port(self, dev_name: str, fp_name: str, keep_peer_id: int | None = None) -> None:
        self._delete_jump_on_port(dev_name, fp_name, keep_peer_id)

    def _delete_patch_only_on_port(self, dev_name: str, fp_name: str, keep_peer_id: int | None = None) -> None:
        """Remove PATCH cables on a port; keep cross-ODF LINK cables (e.g. jump leg on 1-2)."""
        for _ in range(8):
            fp_id = self.fp_id(dev_name, fp_name)
            fp = self.client.request("GET", f"/dcim/front-ports/{fp_id}/")
            removed = False
            for cable in [fp.get("cable"), *[p.get("cable") for p in fp.get("link_peers") or []]]:
                if not isinstance(cable, dict) or not cable.get("id"):
                    continue
                label = (cable.get("label") or "").strip()
                if not label.startswith("PATCH-"):
                    continue
                self.delete_cable_resolving_deps(int(cable["id"]))
                print(f"  移除跳线: {dev_name} {fp_name} ({label})")
                removed = True
                break
            if not removed:
                break

    def _delete_link_only_on_port(self, dev_name: str, fp_name: str, keep_peer_id: int | None = None) -> None:
        """Remove LINK cables on a port; keep same-ODF PATCH (e.g. cross-fiber 1-1→1-2)."""
        for _ in range(8):
            fp_id = self.fp_id(dev_name, fp_name)
            fp = self.client.request("GET", f"/dcim/front-ports/{fp_id}/")
            removed = False
            for cable in [fp.get("cable"), *[p.get("cable") for p in fp.get("link_peers") or []]]:
                if not isinstance(cable, dict) or not cable.get("id"):
                    continue
                if keep_peer_id:
                    keep = False
                    for peer in fp.get("link_peers") or []:
                        if peer.get("id") == keep_peer_id:
                            peer_cab = peer.get("cable") or fp.get("cable")
                            if isinstance(peer_cab, dict) and int(peer_cab.get("id", 0)) == int(cable["id"]):
                                keep = True
                                break
                    if keep:
                        continue
                label = (cable.get("label") or "").strip()
                if not label.startswith("LINK-"):
                    continue
                self.delete_cable_resolving_deps(int(cable["id"]))
                print(f"  移除跳线: {dev_name} {fp_name} ({label})")
                removed = True
                break
            if not removed:
                break

    def ensure_patch(self, dev_name: str, fp_a: str, fp_b: str) -> None:
        if self.ports_linked(dev_name, fp_a, fp_b):
            print(f"  跳线已存在: {dev_name} {fp_a} <-> {fp_b}")
            return
        a = self.fp_id(dev_name, fp_a)
        b = self.fp_id(dev_name, fp_b)
        self._delete_patch_only_on_port(dev_name, fp_a)
        self._delete_patch_only_on_port(dev_name, fp_b)
        if self.ports_linked(dev_name, fp_a, fp_b):
            return
        label = re.sub(r"[^\w\-]", "-", f"PATCH-{dev_name}-{fp_a}-{fp_b}")[:100]
        hits = self.client.request("GET", "/dcim/cables/", params={"label": label, "limit": 1})
        if hits.get("results"):
            return
        a = self.fp_id(dev_name, fp_a)
        b = self.fp_id(dev_name, fp_b)
        self.client.request(
            "POST",
            "/dcim/cables/",
            json={
                "type": "smf",
                "status": "connected",
                "label": label,
                "a_terminations": [{"object_type": "dcim.frontport", "object_id": a}],
                "b_terminations": [{"object_type": "dcim.frontport", "object_id": b}],
            },
        )
        print(f"  同机房跳纤: {dev_name} {fp_a} <-> {fp_b}")

    def ports_linked_between(self, dev_a: str, fp_a: str, dev_b: str, fp_b: str) -> bool:
        a = self.fp_id(dev_a, fp_a)
        b = self.fp_id(dev_b, fp_b)
        fp = self.client.request("GET", f"/dcim/front-ports/{a}/")
        for peer in fp.get("link_peers") or []:
            if peer.get("id") == b:
                return True
        return False

    def ensure_front_link(self, dev_a: str, fp_a: str, dev_b: str, fp_b: str, note: str = "") -> None:
        if dev_a == dev_b:
            self.ensure_patch(dev_a, fp_a, fp_b)
            return
        if self.ports_linked_between(dev_a, fp_a, dev_b, fp_b):
            print(f"  端口连接已存在: {dev_a}:{fp_a} <-> {dev_b}:{fp_b}")
            return
        a = self.fp_id(dev_a, fp_a)
        b = self.fp_id(dev_b, fp_b)
        self._delete_link_only_on_port(dev_a, fp_a, keep_peer_id=b)
        self._delete_link_only_on_port(dev_b, fp_b, keep_peer_id=a)
        if self.ports_linked_between(dev_a, fp_a, dev_b, fp_b):
            return
        label = re.sub(r"[^\w\-]", "-", f"LINK-{dev_a}-{fp_a}-{dev_b}-{fp_b}")[:100]
        hits = self.client.request("GET", "/dcim/cables/", params={"label": label, "limit": 1})
        if hits.get("results"):
            # Stale label with no live terminations previously skipped creating the jump.
            if self.ports_linked_between(dev_a, fp_a, dev_b, fp_b):
                return
            try:
                self.delete_cable_resolving_deps(int(hits["results"][0]["id"]))
            except Exception:
                pass
            if self.ports_linked_between(dev_a, fp_a, dev_b, fp_b):
                return
        self.client.request(
            "POST",
            "/dcim/cables/",
            json={
                "type": "smf",
                "status": "connected",
                "label": label,
                "a_terminations": [{"object_type": "dcim.frontport", "object_id": a}],
                "b_terminations": [{"object_type": "dcim.frontport", "object_id": b}],
            },
        )
        suffix = f" ({note})" if note else ""
        print(f"  跨框连接: {dev_a}:{fp_a} <-> {dev_b}:{fp_b}{suffix}")

    def _resolve_fp_name(self, dev_name: str, fiber: str, cable_label: str) -> str:
        """Resolve front-port name; tolerate mirror cable labels on same ODF."""
        fiber = (fiber or "").strip()
        cable_label = (cable_label or "").strip()
        candidates: list[str] = []
        if cable_label:
            for cab in cable_label_variants(cable_label):
                name = port_name_for_cable_fiber(cab, fiber, local_room=local_room_from_odf_name(dev_name))
                if name not in candidates:
                    candidates.append(name)
        if fiber not in candidates:
            candidates.append(fiber)
        last_err = ""
        for name in candidates:
            try:
                self.fp_id(dev_name, name)
                return name
            except RuntimeError as exc:
                last_err = str(exc)
        raise RuntimeError(last_err or f"找不到端口 {dev_name}:{fiber} @ {cable_label}")

    def apply_port_links(self, links: list[dict[str, str]]) -> None:
        for lk in sort_links_for_apply(links):
            da = lk["dev_a"]
            db = lk["dev_b"]
            pa = self._resolve_fp_name(da, lk["port_a"], lk.get("cab_a", ""))
            pb = self._resolve_fp_name(db, lk["port_b"], lk.get("cab_b", ""))
            note = lk.get("note", "")
            if da == db:
                self.ensure_patch(da, pa, pb)
            else:
                self.ensure_front_link(da, pa, db, pb, note)

    def assign_ports_to_tray(self, dev_name: str, fp_names: list[str]) -> None:
        tray = self.ensure_module(dev_name)
        for name in fp_names:
            fp_id = self.fp_id(dev_name, name)
            fp = self.client.request("GET", f"/dcim/front-ports/{fp_id}/")
            mod = fp.get("module")
            mod_id = mod["id"] if isinstance(mod, dict) else mod
            if mod_id != tray:
                self.client.request("PATCH", f"/dcim/front-ports/{fp_id}/", json={"module": tray})

    def _resolve_splice_plan(
        self,
        did: int,
        dev_name: str,
        plan_name: str,
        fiber_ids: set[int],
    ) -> dict[str, Any]:
        hits = self.client.request("GET", "/plugins/fms/splice-plans/", params={"closure_id": did, "limit": 20})
        plans = hits.get("results", [])

        def _pid(v: Any) -> int:
            return v["id"] if isinstance(v, dict) else int(v)

        if plan_name:
            plan = next((p for p in plans if p["name"] == plan_name), None)
            if plan:
                return plan

        for p in plans:
            entries = self.client.request(
                "GET", "/plugins/fms/splice-plan-entries/", params={"plan_id": p["id"], "limit": 200}
            )
            for e in entries.get("results", []):
                if _pid(e["fiber_a"]) in fiber_ids or _pid(e["fiber_b"]) in fiber_ids:
                    if plan_name and p["name"] != plan_name:
                        p = self.client.request(
                            "PATCH",
                            f"/plugins/fms/splice-plans/{p['id']}/",
                            json={"name": plan_name},
                        )
                        print(f"  熔接方案: {p['name']}")
                    return p

        name = plan_name or f"{dev_name}-熔接方案"
        plan = self.client.request(
            "POST",
            "/plugins/fms/splice-plans/",
            json={"closure": did, "name": name, "description": "Excel 导入"},
        )
        print(f"  熔接方案: {name}")
        return plan

    def ensure_splice_entries(
        self,
        dev_name: str,
        plan_name: str,
        entries: list[tuple[str, str, str]],
    ) -> None:
        fp_names = sorted({a for a, b, _ in entries} | {b for a, b, _ in entries})
        self.assign_ports_to_tray(dev_name, fp_names)
        did = self.dev_id(dev_name)
        fiber_ids = {
            self.fp_id(dev_name, fp)
            for fp_a, fp_b, _ in entries
            for fp in (fp_a, fp_b)
        }
        plan = self._resolve_splice_plan(did, dev_name, plan_name, fiber_ids)
        plan_id = plan["id"]
        tray = self.ensure_module(dev_name)
        existing = self.client.request(
            "GET", "/plugins/fms/splice-plan-entries/", params={"plan_id": plan_id, "limit": 100}
        )

        def _pid(v: Any) -> int:
            return v["id"] if isinstance(v, dict) else int(v)

        pairs = {(_pid(e["fiber_a"]), _pid(e["fiber_b"])) for e in existing.get("results", [])}
        by_fiber_a: dict[int, dict] = {_pid(e["fiber_a"]): e for e in existing.get("results", [])}
        by_fiber_b: dict[int, dict] = {_pid(e["fiber_b"]): e for e in existing.get("results", [])}
        for fp_a, fp_b, note in entries:
            fa, fb = self.fp_id(dev_name, fp_a), self.fp_id(dev_name, fp_b)
            if (fa, fb) in pairs or (fb, fa) in pairs:
                continue
            old = by_fiber_a.get(fa)
            if old and _pid(old["fiber_b"]) == fb:
                continue
            if old and _pid(old["fiber_b"]) != fb:
                self.client.request(
                    "PATCH",
                    f"/plugins/fms/splice-plan-entries/{old['id']}/",
                    json={"fiber_b": fb, "notes": note or old.get("notes", "")},
                )
                print(f"  更新熔接: {fp_a} <-> {fp_b}")
                by_fiber_b[fb] = old
                continue
            old_b = by_fiber_b.get(fb)
            if old_b:
                if _pid(old_b["fiber_a"]) == fa:
                    continue
                self.client.request(
                    "PATCH",
                    f"/plugins/fms/splice-plan-entries/{old_b['id']}/",
                    json={"fiber_a": fa, "notes": note or old_b.get("notes", "")},
                )
                print(f"  更新熔接: {fp_a} <-> {fp_b}")
                continue
            try:
                self.client.request(
                    "POST",
                    "/plugins/fms/splice-plan-entries/",
                    json={"plan": plan_id, "tray": tray, "fiber_a": fa, "fiber_b": fb, "notes": note},
                )
            except RuntimeError as e:
                if "already claimed" in str(e).lower():
                    print(f"  熔接已存在: {fp_a} <-> {fp_b}")
                    continue
                raise
            print(f"  熔接条目: {fp_a} <-> {fp_b}" + (f" ({note})" if note else ""))
            pairs.add((fa, fb))
            by_fiber_a[fa] = {"fiber_a": fa, "fiber_b": fb}
            by_fiber_b[fb] = {"fiber_a": fa, "fiber_b": fb}

    def apply_from_excel(
        self,
        segments: list[dict[str, str]],
        splices: list[dict[str, str]] | None = None,
        links: list[dict[str, str]] | None = None,
        link_mode: str = "port",
        skip_cable_provision: bool = False,
    ) -> None:
        cable_labels: list[str] = []
        for seg in segments:
            cab = seg.get("cable", "").strip()
            if not cab:
                cab = self.infer_cable_label(seg["dev_a"], seg["dev_b"]) or ""
                if cab:
                    seg["cable"] = cab
            if cab:
                cable_labels.append(cab)
        for sp in splices or []:
            if sp.get("cab_a"):
                cable_labels.append(sp["cab_a"])
            if sp.get("cab_b"):
                cable_labels.append(sp["cab_b"])
        for lk in links or []:
            if lk.get("cab_a"):
                cable_labels.append(lk["cab_a"])
            if lk.get("cab_b"):
                cable_labels.append(lk["cab_b"])

        if not skip_cable_provision:
            device_cables = build_device_cables(segments, splices=splices, links=links)

            print("==> 多缆线端口")
            for dev, cabs in device_cables.items():
                for cab in cabs:
                    self.provision_cable(dev, cab)

            print("==> 缆线改接 FMS 后端口")
            for cab in sorted(set(cable_labels)):
                self.fix_cable_termination(cab)

            print("==> 端口重命名（英文缩写格式）")
            for dev, cabs in device_cables.items():
                self.rename_cable_front_ports(dev, cabs)
            self.apply_fiber_port_names(segments, splices=splices, links=links)

        if link_mode == "port":
            print("==> 端口对端口连接")
            self.apply_port_links(links or [])
            if splices:
                print("==> 异纤熔接")
                by_dev: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
                for sp in splices:
                    dev = sp["dev"]
                    pa = resolve_port_name(sp["port_a"], sp.get("cab_a", ""), device_name=dev)
                    pb = resolve_port_name(sp["port_b"], sp.get("cab_b", ""), device_name=dev)
                    by_dev[dev].append((pa, pb, sp.get("note", "")))
                for dev, entries in by_dev.items():
                    self.ensure_splice_entries(dev, f"{dev}-熔接方案", entries)
        else:
            print("==> 熔接/跳线")
            by_plan: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
            for sp in splices or []:
                dev = sp["dev"]
                plan = sp.get("plan") or f"{dev}-熔接方案"
                pa = resolve_port_name(sp["port_a"], sp.get("cab_a", ""))
                pb = resolve_port_name(sp["port_b"], sp.get("cab_b", ""))
                self.ensure_patch(dev, pa, pb)
                by_plan[(dev, plan)].append((pa, pb, sp.get("note", "")))

            for (dev, plan), entries in by_plan.items():
                self.ensure_splice_entries(dev, plan, entries)

    def ensure_strand_loss_fields(self) -> None:
        """Ensure custom fields for per-strand measured loss exist on FiberStrand."""
        existing = {
            x["name"]
            for x in self.client.request("GET", "/extras/custom-fields/", params={"limit": 200}).get("results", [])
        }
        specs = [
            ("measured_loss_db", "实测光损(dB)", "decimal"),
            ("test_wavelength_nm", "测试波长(nm)", "integer"),
        ]
        for name, label, typ in specs:
            if name in existing:
                continue
            self.client.request(
                "POST",
                "/extras/custom-fields/",
                json={
                    "name": name,
                    "label": label,
                    "type": typ,
                    "object_types": ["netbox_fms.fiberstrand"],
                    "required": False,
                },
            )
            print(f"  创建纤芯自定义字段: {label}")

    def ensure_fiber_chain_field(self) -> None:
        existing = {
            x["name"]
            for x in self.client.request("GET", "/extras/custom-fields/", params={"limit": 200}).get("results", [])
        }
        specs = [
            ("fiber_chain", "完整纤芯链", "longtext"),
            ("fiber_chain_detail", "纤芯链明细", "longtext"),
        ]
        for name, label, typ in specs:
            if name in existing:
                continue
            self.client.request(
                "POST",
                "/extras/custom-fields/",
                json={
                    "name": name,
                    "label": label,
                    "type": typ,
                    "object_types": ["netbox_fms.fibercircuitpath"],
                    "required": False,
                },
            )
            print(f"  创建路径自定义字段: {label}")

    def apply_fiber_chain(
        self, path_id: int, trace: dict[str, Any] | None = None
    ) -> tuple[str, str]:
        self.ensure_fiber_chain_field()
        if trace is None:
            trace = self.client.request("GET", f"/plugins/fms/fiber-circuit-paths/{path_id}/trace/")
        chain, detail = build_fiber_chain_from_trace(trace)
        if not chain:
            return "", ""
        self.client.request(
            "PATCH",
            f"/plugins/fms/fiber-circuit-paths/{path_id}/",
            json={"custom_fields": {"fiber_chain": chain, "fiber_chain_detail": detail}},
        )
        return chain, detail

    def find_strand(self, cable_label: str, port: str) -> dict[str, Any]:
        fc = self.fc_id(cable_label)
        candidates = [
            port_name_for_cable_fiber(cable_label, port),
            port,
        ]
        parts = port.split("-")
        if len(parts) >= 2:
            candidates.append(port_name_for_cable_fiber(cable_label, f"{parts[0]}-{parts[-1]}"))

        strands = self.client.request(
            "GET", "/plugins/fms/fiber-strands/", params={"fiber_cable_id": fc, "limit": 200}
        )
        by_name = {s["name"]: s for s in strands.get("results", [])}
        for name in candidates:
            if name in by_name:
                return by_name[name]

        try:
            pos = position_from_fiber(port)
        except (ValueError, IndexError):
            pos = None
        if pos is not None:
            for s in strands.get("results", []):
                if s["position"] == pos:
                    return s

        raise RuntimeError(f"缆段 {cable_label} 上找不到纤芯 {port}（尝试过 {', '.join(candidates)}）")

    def apply_strand_losses(self, rows: list[dict[str, str]]) -> int:
        if not rows:
            return 0
        self.ensure_strand_loss_fields()
        updated = 0
        for row in rows:
            cable = (row.get("cable") or "").strip()
            port = (row.get("port") or "").strip()
            loss = (row.get("loss_db") or "").strip()
            if not cable or not port or not loss:
                continue
            strand = self.find_strand(cable, port)
            wl = (row.get("wavelength") or "1310").strip()
            payload: dict[str, Any] = {
                "custom_fields": {
                    "measured_loss_db": float(loss) if loss.replace(".", "", 1).isdigit() else loss,
                    "test_wavelength_nm": int(wl) if wl else 1310,
                }
            }
            self.client.request("PATCH", f"/plugins/fms/fiber-strands/{strand['id']}/", json=payload)
            odf = (row.get("odf") or "").strip()
            label = f"{odf} " if odf else ""
            print(f"  纤芯光损: {label}{port} @ {cable} -> {loss}dB@{wl}nm (strand #{strand['id']})")
            updated += 1
        return updated

    def retrace_and_verify(
        self,
        cids: list[str],
        expected: dict[str, tuple[int, int, str, str]] | None = None,
    ) -> list[str]:
        """expected: cid -> (origin_id, dest_id, dest_dev, dest_port_name)"""
        failures: list[str] = []
        for cid in cids:
            circ = self.client.request("GET", "/plugins/fms/fiber-circuits/", params={"cid": cid, "limit": 1})
            if not circ.get("results"):
                failures.append(f"{cid}: 光路不存在")
                continue
            circuit_id = circ["results"][0]["id"]
            exp = (expected or {}).get(cid)
            self.client.request("POST", f"/plugins/fms/fiber-circuits/{circuit_id}/retrace/")
            paths = self.client.request(
                "GET", "/plugins/fms/fiber-circuit-paths/", params={"circuit_id": circuit_id, "limit": 5}
            )
            if exp:
                origin_id, dest_id, dest_dev, dest_port = exp
                for path in paths.get("results", []):
                    self.client.request(
                        "PATCH",
                        f"/plugins/fms/fiber-circuit-paths/{path['id']}/",
                        json={"origin": origin_id, "destination": dest_id},
                    )
                paths = self.client.request(
                    "GET", "/plugins/fms/fiber-circuit-paths/", params={"circuit_id": circuit_id, "limit": 5}
                )
            for path in paths.get("results", []):
                fresh = self.client.request("GET", f"/plugins/fms/fiber-circuit-paths/{path['id']}/")
                ok = fresh.get("is_complete")
                hops = len(fresh.get("path") or [])
                status = "完整" if ok else "未完整"
                print(f"  {cid}: {status}, hops={hops}")
                if not ok:
                    failures.append(f"{cid}: 路径未完整 (hops={hops})")
                    continue
                if exp:
                    dest = fresh.get("destination")
                    dest_id = dest["id"] if isinstance(dest, dict) else dest
                    if dest_id != exp[1]:
                        fp = self.client.request("GET", f"/dcim/front-ports/{dest_id}/")
                        failures.append(
                            f"{cid}: 终点端口ID不符 期望表 {dest_dev}:{dest_port} "
                            f"实际 {fp['device']['name']}:{fp['name']}"
                        )
        return failures
