#!/usr/bin/env python3
"""ODF browser: site -> room -> cabinet -> ODF tree and port grid."""

from __future__ import annotations

import re
import sys
import threading
import time
from collections import defaultdict
from typing import Any

from field_service import (
    build_fp_circuit_index,
    fp_index_for_targets,
    cables_at_odf,
    client_from_config,
    default_fiber_options,
    describe_front_port_local_from_fp,
    fiber_from_port_name,
    infer_cable_between,
    is_odf_device,
    list_odf_devices,
    load_config,
    load_store,
    local_segment_at_odf,
    mirror_cable_label,
    neighbor_odfs,
    paginate,
    resolve_peer_from_cable,
)
from import_from_excel import find_device
from naming_rules import (
    cable_label_variants,
    fiber_sort_key,
    is_bootstrap_tmp_cable,
    is_trunk_cable_label,
    local_room_from_odf_name,
    normalize_fiber_label,
    parse_route_label,
    room_route_short,
)
from field_cache import (
    ROOM_PANEL_MEM_TTL_SEC,
    TREE_MEM_TTL_SEC,
    load_room_panel_entry,
    invalidate_room_panel_disk,
    list_warmed_room_keys,
    load_room_panel_from_disk,
    load_tree_from_disk,
    save_room_panel_to_disk,
    save_tree_to_disk,
)
from portal_settings import apply_route_policy

ODF_NAME_RE = re.compile(r"^(.+?)-([A-Za-z0-9]+)-ODF-(\d+)$")
CABLE_TYPE_KEYWORDS = ("监控", "生产", "备用", "专网", "SCADA")
_TREE_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_TREE_TTL_SEC = TREE_MEM_TTL_SEC
_ROOM_PANEL_CACHE: dict[str, dict[str, Any]] = {}
_ROOM_PANEL_TTL_SEC = ROOM_PANEL_MEM_TTL_SEC
_ROOM_REFRESH_INFLIGHT: set[str] = set()
_ROOM_REFRESH_LOCK = threading.Lock()


def _room_panel_cache_key(site_name: str, room_name: str) -> str:
    return f"{site_name}|{room_name}"


def invalidate_room_panel_cache(*, site: str = "", room: str = "") -> None:
    if not site:
        _ROOM_PANEL_CACHE.clear()
        invalidate_room_panel_disk()
        return
    key = _room_panel_cache_key(site, room) if room else None
    if key:
        _ROOM_PANEL_CACHE.pop(key, None)
    else:
        prefix = f"{site}|"
        for k in list(_ROOM_PANEL_CACHE.keys()):
            if k.startswith(prefix):
                _ROOM_PANEL_CACHE.pop(k, None)
    invalidate_room_panel_disk(site=site, room=room)


def _rear_port_for_cable(client, device_id: int, cable_label: str) -> dict[str, Any] | None:
    from naming_rules import cable_label_variants

    for variant in cable_label_variants(cable_label):
        hits = client.request(
            "GET",
            "/dcim/rear-ports/",
            params={"device_id": device_id, "name": variant, "limit": 1},
        )
        if hits.get("results"):
            return client.request("GET", f"/dcim/rear-ports/{hits['results'][0]['id']}/")
    return None


def _rear_ports_matching(rear_ports: list[dict[str, Any]], cable_label: str) -> list[dict[str, Any]]:
    from naming_rules import cable_label_variants

    variants = set(cable_label_variants(cable_label))
    return [rp for rp in rear_ports if (rp.get("name") or "").strip() in variants]


def _rear_port_match(rear_ports: list[dict[str, Any]], cable_label: str) -> dict[str, Any] | None:
    return _best_rear_port_for_cable(rear_ports, cable_label)


def _best_rear_port_for_cable(
    rear_ports: list[dict[str, Any]],
    cable_label: str,
    fps_by_rear: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Pick rear section with most front ports (A/B 端 rear 名不同，B 端 front 常在缆线名 rear 上)."""
    matched = _rear_ports_matching(rear_ports, cable_label)
    if not matched:
        return None
    if len(matched) == 1:
        return matched[0]

    def _score(rp: dict[str, Any]) -> tuple[int, int]:
        rp_id = int(rp["id"])
        front = len((fps_by_rear or {}).get(rp_id, []))
        pos = int(rp.get("positions") or 0)
        return (front, pos)

    return max(matched, key=_score)


def _fps_by_rear_for_device(fps: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    by_rear: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for fp in fps:
        rp = fp.get("rear_port") or {}
        rp_id = rp.get("id") if isinstance(rp, dict) else rp
        if rp_id:
            by_rear[int(rp_id)].append(fp)
    return by_rear


def _linked_fps_for_cable(
    client,
    *,
    cable_label: str,
    section_cable: str,
    fp_by_id: dict[int, dict[str, Any]],
    rear_ports: list[dict[str, Any]],
    fps_by_rear: dict[int, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], str]:
    """Try every matching rear; B 端 list API 常不带 front.rear_port，需读 rear 详情."""
    matched = _rear_ports_matching(rear_ports, cable_label)
    if not matched:
        return [], section_cable or cable_label

    best_fps: list[dict[str, Any]] = []
    best_section = section_cable or cable_label
    for rp in matched:
        sec = (rp.get("name") or cable_label).strip()
        linked = list(fps_by_rear.get(int(rp["id"]), []))
        if not linked:
            rp_detail = client.request("GET", f"/dcim/rear-ports/{rp['id']}/")
            for link in rp_detail.get("front_ports") or []:
                fp_id = link.get("front_port")
                fp = fp_by_id.get(fp_id)
                if fp:
                    linked.append(fp)
        if len(linked) > len(best_fps):
            best_fps = linked
            best_section = sec

    best_fps.sort(
        key=lambda fp: fiber_sort_key(
            _fiber_label_from_port(
                fp.get("name") or "", best_section, [best_section], fp.get("position")
            )
        )
    )
    return best_fps, best_section


def _build_route_port_grid(
    client,
    entry: dict[str, Any],
    fp_by_id: dict[int, dict[str, Any]],
    *,
    web_base: str,
    device_meta: dict[str, str] | None = None,
    rear_ports: list[dict[str, Any]] | None = None,
    fps_by_rear: dict[int, list[dict[str, Any]]] | None = None,
    rear_by_dev: dict[int, list[str]] | None = None,
    fp_index: dict[int, list[dict[str, Any]]] | None = None,
    include_loss: bool = True,
) -> dict[str, Any]:
    """Fast port grid for one route in room overview (shared circuit index)."""
    odf_name = entry["name"]
    cable_label = (entry.get("cable") or "").strip()
    did = int(entry["id"]) if str(entry.get("id", "")).isdigit() else 0
    parsed = parse_odf_name(odf_name)
    if device_meta:
        room_name = device_meta.get("room") or parsed["room"]
        site = device_meta.get("site") or ""
        device_url = device_meta.get("url") or f"{web_base.rstrip('/')}/dcim/devices/{did}/"
        connector_type = device_meta.get("connector_type") or ""
        connector_label = device_meta.get("connector_label") or ""
        if device_url.startswith("/"):
            device_url = f"{web_base.rstrip('/')}{device_url}"
    else:
        dev = find_device(client, odf_name)
        did = dev["id"]
        loc = dev.get("location") or {}
        room_name = (loc.get("name", "") if isinstance(loc, dict) else "") or parsed["room"]
        site = (dev.get("site") or {}).get("name", "")
        device_url = f"{web_base.rstrip('/')}/dcim/devices/{did}/"
        connector_type = _connector_type_from_device(dev) or ""
        connector_label = _connector_label_from_device(dev) or ""

    peer_odf = entry.get("peer_odf") or ""
    peer_display = entry.get("peer_display") or entry.get("peer_room") or peer_odf
    if not peer_display and cable_label:
        peer_display = peer_room_from_cable_label(cable_label, room_name)

    ports: list[dict[str, Any]] = []
    section_cable = cable_label
    display_cable = _display_cable_label(cable_label, room_name)
    if cable_label:
        rp = _best_rear_port_for_cable(rear_ports or [], cable_label, fps_by_rear) if rear_ports is not None else None
        if rp is None and rear_ports is None:
            rp = _rear_port_for_cable(client, did, cable_label)
        if rp:
            section_cable = (rp.get("name") or cable_label).strip()
            if fps_by_rear is not None and rear_ports is not None:
                linked_fps, section_cable = _linked_fps_for_cable(
                    client,
                    cable_label=cable_label,
                    section_cable=section_cable,
                    fp_by_id=fp_by_id,
                    rear_ports=rear_ports,
                    fps_by_rear=fps_by_rear,
                )
            else:
                linked_ids = [link.get("front_port") for link in (rp.get("front_ports") or []) if link.get("front_port")]
                linked_fps = [fp_by_id[fid] for fid in linked_ids if fid in fp_by_id]
                linked_fps.sort(
                    key=lambda fp: fiber_sort_key(
                        _fiber_label_from_port(
                            fp.get("name") or "", section_cable, [section_cable], fp.get("position")
                        )
                    )
                )
            loss_by_fiber = _strand_loss_map(client, section_cable or cable_label) if include_loss else {}
            for fp in linked_fps:
                fp_id = fp["id"]
                name = fp.get("name") or f"pos-{fp_id}"
                fiber = _fiber_label_from_port(
                    name,
                    display_cable or section_cable,
                    [display_cable, section_cable, cable_label],
                    fp.get("position"),
                    local_room=room_name,
                )
                peers = fp.get("link_peers") or []
                linked = len(peers) > 0
                has_jump = linked or bool((fp.get("cable") or {}).get("id") if isinstance(fp.get("cable"), dict) else fp.get("cable"))
                circuits = (fp_index or {}).get(fp_id, []) if fp_index is not None else []
                in_use = len(circuits) > 0
                primary = circuits[0] if circuits else {}
                cid = (primary.get("cid") or "").strip()
                loss_info = loss_by_fiber.get(fiber) or loss_by_fiber.get(f"1-{fp.get('position') or 0}") or {}
                loss_db = loss_info.get("loss_db", "")
                ports.append(
                    {
                        "id": fp_id,
                        "position": fp.get("position") or 0,
                        "name": name,
                        "fiber": fiber,
                        "cable": display_cable or section_cable,
                        "linked": linked,
                        "peer_names": [p.get("name") for p in peers if isinstance(p, dict) and p.get("name")],
                        "has_jump": has_jump,
                        "loss_db": loss_db,
                        "empty": not in_use and not linked and not has_jump and not loss_db,
                        "in_use": in_use,
                        "cid": cid,
                        "local_summary": cid if in_use else ("跳纤" if has_jump else ""),
                        "peer_odf": peer_odf,
                    }
                )

    if not ports and cable_label:
        rp = _best_rear_port_for_cable(rear_ports or [], cable_label, fps_by_rear) if rear_ports else None
        strand_count = int(rp.get("positions") or 12) if rp else 12
        loss_by_fiber = _strand_loss_map(client, section_cable or cable_label) if include_loss else {}
        for i, f in enumerate(default_fiber_options(strand_count)):
            loss_info = loss_by_fiber.get(f, {})
            loss_db = loss_info.get("loss_db", "")
            ports.append(
                {
                    "id": None,
                    "position": i + 1,
                    "name": "",
                    "fiber": f,
                    "cable": display_cable or cable_label,
                    "linked": False,
                    "has_jump": False,
                    "loss_db": loss_db,
                    "empty": not loss_db,
                    "in_use": False,
                    "cid": "",
                    "local_summary": "",
                    "peer_odf": peer_odf,
                }
            )

    cables_sections = [
        {
            "cable": display_cable or section_cable or cable_label,
            "peer_odf": peer_odf,
            "peer_display_name": peer_display,
            "ports": ports,
        }
    ] if (section_cable or cable_label) else []

    return {
        "odf": odf_name,
        "device_id": did,
        "display_name": entry.get("display_name") or odf_name,
        "route_key": entry.get("route_key") or odf_name,
        "cable_filter": cable_label,
        "site": site,
        "room": room_name,
        "device_url": device_url,
        "connector_type": connector_type,
        "connector_label": connector_label or _CONNECTOR_LABELS.get(connector_type, ""),
        "cables": _sections_for_cable_filter(cables_sections, cable_label) if cable_label else cables_sections,
        "fibers": sorted({p["fiber"] for s in cables_sections for p in s.get("ports", []) if p.get("fiber")}, key=fiber_sort_key),
        "overview": True,
    }


def _sections_for_cable_filter(sections: list[dict[str, Any]], cable_filter: str) -> list[dict[str, Any]]:
    """Match route cable to rear-port sections (A/B 端 rear 名可能不同，取有实际 front 口的一节)."""
    if not cable_filter:
        return sections
    variants = set(cable_label_variants(cable_filter))
    matched = [s for s in sections if s.get("cable") in variants]
    if not matched:
        return []
    if len(matched) == 1:
        return matched

    def _score(section: dict[str, Any]) -> tuple[int, int]:
        ports = section.get("ports") or []
        real = sum(1 for p in ports if p.get("id"))
        return (real, len(ports))

    return [max(matched, key=_score)]


def _infer_cable_type_suffix_light(cable_label: str) -> str:
    blob = cable_label or ""
    for kw in CABLE_TYPE_KEYWORDS:
        if kw in blob:
            return kw
    return ""


_ODF_RECORDS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_ODF_RECORDS_TTL_SEC = 3600
_REAR_BY_DEV_TTL_SEC = 3600


def _rear_ports_by_device_id(client) -> dict[int, list[str]]:
    now = time.time()
    cache = getattr(_rear_ports_by_device_id, "_cache", None)
    if cache and now - cache.get("ts", 0) < _REAR_BY_DEV_TTL_SEC:
        return cache["data"]
    by_dev: dict[int, list[str]] = {}
    for rp in paginate(client, "/dcim/rear-ports/"):
        dev = rp.get("device") or {}
        did = dev.get("id") if isinstance(dev, dict) else None
        lbl = (rp.get("name") or "").strip()
        if did and is_trunk_cable_label(lbl):
            by_dev.setdefault(int(did), []).append(lbl)
    for did in by_dev:
        by_dev[did] = sorted(set(by_dev[did]))
    _rear_ports_by_device_id._cache = {"ts": now, "data": by_dev}
    return by_dev


def room_short(name: str) -> str:
    """轧钢机房 -> 轧钢；白灰窑2机房 -> 白灰窑2"""
    s = (name or "").strip()
    if s.endswith("机房"):
        return s[:-2]
    if "机房" in s:
        return s.replace("机房", "").strip("- ")
    return s.split("-")[0] if "-" in s else s


def peer_room_from_cable(client, odf_name: str, cable_label: str, local_room: str) -> str:
    peer_odf = resolve_peer_from_cable(client, odf_name, cable_label)
    if peer_odf:
        peer_dev = find_device(client, peer_odf)
        loc = peer_dev.get("location") or {}
        peer_room = loc.get("name", "") if isinstance(loc, dict) else ""
        if not peer_room:
            peer_room = parse_odf_name(peer_odf).get("room", "")
        if peer_room:
            return room_short(peer_room)
    return peer_room_from_cable_label(cable_label, local_room)


def peer_room_from_cable_label(cable_label: str, local_room: str) -> str:
    lbl = (cable_label or "").strip()
    p = parse_route_label(lbl)
    if p["room_a"] and p["room_b"] and "至" in lbl:
        ra, rb = normalize_room(p["room_a"]), normalize_room(p["room_b"])
        local = normalize_room(local_room)
        if local == ra:
            return room_short(rb)
        if local == rb:
            return room_short(ra)
        if _room_matches(local, ra):
            return room_short(rb)
        if _room_matches(local, rb):
            return room_short(ra)
        return room_short(rb)
    if lbl.upper().startswith("CBL-"):
        lbl = lbl[4:]
    parts = [p for p in lbl.split("-") if p]
    if len(parts) < 2:
        return parts[0] if parts else "对端"
    local_s = room_short(local_room)
    a, b = parts[0], parts[1]
    if a in local_s or local_s.startswith(a) or local_s.endswith(a):
        return b
    if b in local_s or local_s.startswith(b) or local_s.endswith(b):
        return a
    return b


def normalize_room(name: str) -> str:
    from naming_rules import normalize_room_name

    return normalize_room_name(name)


def _room_matches(a: str, b: str) -> bool:
    na, nb = normalize_room(a), normalize_room(b)
    if na and nb and na == nb:
        return True
    sa, sb = room_short(na), room_short(nb)
    return bool(sa and sb and sa == sb)


def _display_cable_label(cable_label: str, local_room: str) -> str:
    """本端视角缆段名：B 端 front 常挂在对端 rear 名上，展示用镜像缆段."""
    from naming_rules import mirror_cable_label, parse_route_label

    lbl = (cable_label or "").strip()
    if not lbl or not local_room:
        return lbl
    p = parse_route_label(lbl)
    if not (p["room_a"] and p["room_b"] and "至" in lbl):
        return lbl
    local = normalize_room(local_room)
    ra, rb = normalize_room(p["room_a"]), normalize_room(p["room_b"])
    if _room_matches(local, rb) and not _room_matches(local, ra):
        mirrored = mirror_cable_label(lbl)
        return mirrored if mirrored and mirrored != lbl else lbl
    return lbl


def _infer_cable_type_suffix(client, odf_name: str, cable_label: str) -> str:
    text_parts = [cable_label]
    for cab in paginate(client, "/dcim/cables/"):
        if (cab.get("label") or "").strip() == cable_label:
            if cab.get("description"):
                text_parts.append(str(cab["description"]))
            break
    try:
        hits = client.request("GET", "/plugins/fms/fiber-cables/", params={"limit": 100})
        for fc in hits.get("results", []):
            cab = fc.get("cable") or {}
            lbl = cab.get("label") if isinstance(cab, dict) else ""
            if lbl == cable_label:
                if fc.get("comments"):
                    text_parts.append(str(fc["comments"]))
                break
    except Exception:
        pass
    blob = " ".join(text_parts)
    for kw in CABLE_TYPE_KEYWORDS:
        if kw in blob:
            return kw
    import re as _re

    m = _re.search(r"FIB-[^-]+-(\S+)", blob)
    if m and m.group(1) in CABLE_TYPE_KEYWORDS:
        return m.group(1)
    return ""


def _cable_sort_key(local_room: str, cable_label: str) -> tuple[int, str]:
    """Prefer outbound cable (local endpoint first) as default route name."""
    local_s = room_short(local_room)
    lbl = (cable_label or "").strip()
    p = parse_route_label(lbl)
    if p["room_a"] and p["room_b"] and "至" in lbl:
        first = room_short(p["room_a"])
        primary = 0 if first and (first in local_s or local_s.startswith(first) or local_s.endswith(first)) else 1
        return (primary, cable_label)
    if lbl.upper().startswith("CBL-"):
        lbl = lbl[4:]
    parts = [p for p in lbl.split("-") if p]
    first = parts[0] if parts else ""
    primary = 0 if first and (first in local_s or local_s.startswith(first) or local_s.endswith(first)) else 1
    return (primary, cable_label)


def _cables_on_odf(client, odf_name: str) -> list[str]:
    dev = find_device(client, odf_name)
    did = dev["id"]
    cables: list[str] = []
    for rp in paginate(client, "/dcim/rear-ports/", {"device_id": did}):
        lbl = (rp.get("name") or "").strip()
        if is_trunk_cable_label(lbl):
            cables.append(lbl)
    if not cables:
        cables = [c["label"] for c in cables_at_odf(client, odf_name)]
    return sorted(set(cables))


def _cable_belongs_on_odf_device(odf_name: str, cable: str) -> bool:
    from naming_rules import cable_belongs_on_route_odf

    return cable_belongs_on_route_odf(odf_name, cable)


def _primary_trunk_cables(cables: list[str], odf_name: str, local_room: str) -> list[str]:
    """树/面板用：去掉 TMP 临时缆，同对端只保留一条主缆（优先与 ODF 设备名一致、本端 outbound）。"""
    filtered = [
        c.strip()
        for c in cables
        if c and not is_bootstrap_tmp_cable(c) and _cable_belongs_on_odf_device(odf_name, c.strip())
    ]
    if not filtered:
        return []

    def priority(cab: str) -> float:
        if cab == odf_name:
            return 1000.0
        p = parse_route_label(cab)
        if p["room_a"] and p["room_b"] and "至" in cab:
            local = normalize_room(local_room)
            ra, rb = normalize_room(p["room_a"]), normalize_room(p["room_b"])
            if _room_matches(local, ra):
                return 500.0 - len(cab) * 0.01
            if _room_matches(local, rb):
                return 100.0 - len(cab) * 0.01
        return 50.0 - len(cab) * 0.01

    groups: dict[tuple[str, str], list[str]] = {}
    for cab in filtered:
        peer = peer_room_from_cable_label(cab, local_room)
        suffix = parse_route_label(cab).get("suffix") or _infer_cable_type_suffix_light(cab)
        groups.setdefault((peer, suffix), []).append(cab)
    picked = [max(cabs, key=priority) for cabs in groups.values()]
    return sorted(set(picked), key=lambda c: _cable_sort_key(local_room, c))


def build_odf_route_entries(
    client,
    records: list[dict[str, str]],
    *,
    rear_by_dev: dict[int, list[str]] | None = None,
    light: bool = False,
) -> list[dict[str, Any]]:
    """One selectable ODF route per cable (display: 轧钢至白灰窑 / 轧钢至白灰窑监控)."""
    entries: list[dict[str, Any]] = []
    for rec in records:
        odf_name = rec["name"]
        local_room = rec["room"]
        local_short = room_short(local_room)
        did = int(rec["id"]) if str(rec.get("id", "")).isdigit() else 0
        if rear_by_dev is not None and did:
            cables = list(rear_by_dev.get(did, []))
        else:
            cables = _cables_on_odf(client, odf_name)
        cables = _primary_trunk_cables(cables, odf_name, local_room)
        if not cables:
            entries.append(
                {
                    "name": odf_name,
                    "display_name": odf_name,
                    "cable": "",
                    "peer_room": "",
                    "id": rec["id"],
                    "route_key": odf_name,
                }
            )
            continue
        groups: dict[str, list[str]] = {}
        for cab in cables:
            pr = (
                peer_room_from_cable_label(cab, local_room)
                if light
                else peer_room_from_cable(client, odf_name, cab, local_room)
            )
            groups.setdefault(pr, []).append(cab)
        for peer_short in sorted(groups.keys()):
            cab_list = sorted(set(groups[peer_short]), key=lambda c: _cable_sort_key(local_room, c))
            suffix_by_cab = {
                cab: (_infer_cable_type_suffix_light(cab) if light else _infer_cable_type_suffix(client, odf_name, cab))
                for cab in cab_list
            }
            used_names: set[str] = set()
            for idx, cab in enumerate(cab_list):
                base = f"{local_short}至{peer_short}"
                suffix = suffix_by_cab.get(cab, "")
                if len(cab_list) == 1:
                    display = base
                elif idx == 0 and not suffix:
                    display = base
                elif suffix:
                    display = f"{base}{suffix}"
                else:
                    rp = parse_route_label(cab)
                    tail = rp["suffix"] or room_route_short(rp["room_b"])
                    display = f"{base}{tail}"
                if display in used_names:
                    display = f"{display}·{cab.split('-')[-1]}"
                used_names.add(display)
                entries.append(
                    {
                        "name": odf_name,
                        "display_name": display,
                        "cable": cab,
                        "peer_room": peer_short,
                        "id": rec["id"],
                        "route_key": f"{odf_name}|{cab}",
                    }
                )
    return apply_route_policy(entries)


def attach_route_display_names(client, options: list[dict[str, Any]]) -> None:
    """Add display_name / route_key to ODF option dicts (jump targets, etc.)."""
    if not options:
        return
    rear_by_dev = _rear_ports_by_device_id(client)
    recs = [{"name": o["name"], "room": o.get("room", ""), "id": o.get("id", "")} for o in options]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for entry in build_odf_route_entries(client, recs, rear_by_dev=rear_by_dev, light=True):
        by_name.setdefault(entry["name"], []).append(entry)
    for o in options:
        entries = by_name.get(o["name"], [])
        if entries:
            o["display_name"] = entries[0]["display_name"]
            o["route_key"] = entries[0]["route_key"]
        else:
            o["display_name"] = o["name"]
            o["route_key"] = o["name"]
    # Jump targets: only same-room routes in whitelist
    allowed = None
    try:
        from portal_settings import allowed_route_keys

        allowed = allowed_route_keys()
    except Exception:
        pass
    if allowed is not None:
        options[:] = [o for o in options if o.get("route_key") in allowed or o.get("name") in allowed]


def _display_name_for_route(
    client,
    odf_name: str,
    room: str,
    device_id: int | str,
    cable_filter: str,
    *,
    rear_by_dev: dict[int, list[str]] | None = None,
) -> tuple[str, str]:
    rec = {"name": odf_name, "room": room, "id": str(device_id)}
    rb = rear_by_dev if rear_by_dev is not None else _rear_ports_by_device_id(client)
    for entry in build_odf_route_entries(client, [rec], rear_by_dev=rb, light=True):
        if not cable_filter or entry.get("cable") == cable_filter:
            return entry["display_name"], entry["route_key"]
    return odf_name, odf_name


def _peer_display_name(
    client,
    peer_odf: str,
    cable_label: str,
    *,
    rear_by_dev: dict[int, list[str]] | None = None,
) -> str:
    peer_odf = (peer_odf or "").strip()
    if not peer_odf:
        return ""
    try:
        peer_dev = find_device(client, peer_odf)
    except Exception:
        return peer_odf
    loc = peer_dev.get("location") or {}
    room = (loc.get("name", "") if isinstance(loc, dict) else "") or parse_odf_name(peer_odf).get("room", "")
    rb = rear_by_dev if rear_by_dev is not None else _rear_ports_by_device_id(client)
    rec = {"name": peer_odf, "room": room, "id": str(peer_dev["id"])}
    entries = build_odf_route_entries(client, [rec], rear_by_dev=rb, light=True)
    for cab in (cable_label, mirror_cable_label(cable_label)):
        if not cab:
            continue
        for entry in entries:
            if entry.get("cable") == cab:
                return entry.get("display_name") or peer_odf
    if len(entries) == 1:
        return entries[0].get("display_name") or peer_odf
    name, _ = _display_name_for_route(
        client, peer_odf, room, peer_dev["id"], cable_label, rear_by_dev=rb
    )
    return name or peer_odf


def parse_odf_name(device_name: str) -> dict[str, str]:
    p = parse_route_label(device_name)
    if p["room_a"] and p["room_b"] and "至" in (device_name or ""):
        return {
            "room": p["room_a"],
            "peer": p["room_b"],
            "suffix": p["suffix"],
            "cabinet": "",
            "odf_no": "",
        }
    m = ODF_NAME_RE.match((device_name or "").strip())
    if not m:
        return {"room": device_name, "cabinet": "", "odf_no": "", "peer": "", "suffix": ""}
    return {"room": m.group(1), "cabinet": m.group(2), "odf_no": m.group(3), "peer": "", "suffix": ""}


_CONNECTOR_LABELS = {"FC": "圆口-FC", "SC": "大方-SC"}


def _connector_code_from_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    if "大方" in raw:
        return "SC"
    if "圆口" in raw:
        return "FC"
    upper = raw.upper()
    compact = upper.replace("接口", "").replace("-", "").replace("_", "").replace(" ", "")
    if compact in ("FC", "SC"):
        return compact
    if "SC" in upper and "FC" not in upper:
        return "SC"
    if "FC" in upper:
        return "FC"
    return ""


def _connector_type_from_device(device: dict[str, Any]) -> str:
    """Read FC/SC from NetBox device tags, custom field, or description (ODF frame level)."""
    cf = device.get("custom_fields") or {}
    for key in ("connector_type", "interface_type", "odf_connector"):
        code = _connector_code_from_text(str(cf.get(key) or ""))
        if code:
            return code
    for tag in device.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        for field in ("name", "slug", "display"):
            code = _connector_code_from_text(tag.get(field) or "")
            if code:
                return code
    for field in ("description", "comments"):
        code = _connector_code_from_text(device.get(field) or "")
        if code:
            return code
    return ""


def _connector_label_from_device(device: dict[str, Any]) -> str:
    """Prefer the actual NetBox tag name (圆口-FC / 大方-SC)."""
    for tag in device.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        name = (tag.get("name") or "").strip()
        slug = tag.get("slug") or ""
        if _connector_code_from_text(name) or _connector_code_from_text(slug):
            if name:
                return name
    code = _connector_type_from_device(device)
    return _CONNECTOR_LABELS.get(code, "")


def list_all_odf_records(client, *, force_refresh: bool = False) -> list[dict[str, str]]:
    now = time.time()
    if not force_refresh and _ODF_RECORDS_CACHE.get("data") is not None:
        if now - float(_ODF_RECORDS_CACHE.get("ts", 0)) < _ODF_RECORDS_TTL_SEC:
            return list(_ODF_RECORDS_CACHE["data"])
    records: list[dict[str, str]] = []
    for d in paginate(client, "/dcim/devices/"):
        name = d.get("name") or ""
        if not is_odf_device(d):
            continue
        site = (d.get("site") or {})
        site_name = site.get("name", "") if isinstance(site, dict) else str(site)
        loc = d.get("location") or {}
        loc_name = loc.get("name", "") if isinstance(loc, dict) else ""
        parsed = parse_odf_name(name)
        room = loc_name or parsed["room"] or name.split("-")[0]
        records.append(
            {
                "id": str(d["id"]),
                "name": name,
                "site": site_name or "未分配站点",
                "room": room,
                "cabinet": parsed["cabinet"],
                "odf_no": parsed["odf_no"],
                "url": f"/dcim/devices/{d['id']}/",
                "connector_type": _connector_type_from_device(d),
                "connector_label": _connector_label_from_device(d),
            }
        )
    records = sorted(records, key=lambda x: (x["site"], x["room"], x["cabinet"], x["name"]))
    _ODF_RECORDS_CACHE["ts"] = now
    _ODF_RECORDS_CACHE["data"] = records
    return records


def invalidate_odf_records_cache() -> None:
    _ODF_RECORDS_CACHE["ts"] = 0.0
    _ODF_RECORDS_CACHE["data"] = None
    if hasattr(_rear_ports_by_device_id, "_cache"):
        delattr(_rear_ports_by_device_id, "_cache")
    invalidate_strand_loss_cache()
    try:
        from field_service import invalidate_trace_display_caches

        invalidate_trace_display_caches()
    except Exception:
        pass


def invalidate_strand_loss_cache(cable_label: str = "") -> None:
    cache = getattr(_strand_loss_map, "_cache", None)
    if not isinstance(cache, dict):
        return
    if not cable_label:
        _strand_loss_map._cache = {}
        return
    for alias in (cable_label_variants(cable_label) or [cable_label]):
        cache.pop(alias, None)
    cache.pop(cable_label, None)


def _collect_panel_fp_ids(routes: list[dict[str, Any]]) -> set[int]:
    out: set[int] = set()
    for route in routes:
        for sec in route.get("cables") or []:
            for port in sec.get("ports") or []:
                pid = port.get("id")
                if pid:
                    try:
                        out.add(int(pid))
                    except (TypeError, ValueError):
                        continue
    return out


def _apply_fp_index_to_routes(routes: list[dict[str, Any]], fp_index: dict[int, list[dict[str, Any]]]) -> None:
    for route in routes:
        for sec in route.get("cables") or []:
            for port in sec.get("ports") or []:
                fp_id = port.get("id")
                if not fp_id:
                    continue
                try:
                    fp_key = int(fp_id)
                except (TypeError, ValueError):
                    continue
                circuits = fp_index.get(fp_key, [])
                in_use = len(circuits) > 0
                primary = circuits[0] if circuits else {}
                cid = (primary.get("cid") or "").strip()
                linked = bool(port.get("linked"))
                has_jump = bool(port.get("has_jump") or linked)
                port["in_use"] = in_use
                port["cid"] = cid
                port["empty"] = not in_use and not linked and not has_jump and not (port.get("loss_db") or "")
                port["local_summary"] = cid if in_use else ("跳纤" if has_jump else "")


def _apply_loss_to_routes(client, routes: list[dict[str, Any]]) -> None:
    loss_cache: dict[str, dict[str, dict[str, str]]] = {}
    for route in routes:
        for sec in route.get("cables") or []:
            cable = (sec.get("cable") or "").strip()
            if cable not in loss_cache:
                loss_cache[cable] = _strand_loss_map(client, cable) if cable else {}
            by_fiber = loss_cache[cable]
            for port in sec.get("ports") or []:
                fiber = port.get("fiber") or ""
                pos = port.get("position") or 0
                info = by_fiber.get(fiber) or by_fiber.get(f"1-{pos}")
                if info is not None:
                    loss_db = info.get("loss_db") or ""
                    port["loss_db"] = str(loss_db) if loss_db else ""
                    if info.get("wavelength"):
                        port["wavelength"] = str(info.get("wavelength"))
                elif by_fiber:
                    port["loss_db"] = ""
                port["empty"] = (
                    not port.get("in_use")
                    and not port.get("linked")
                    and not port.get("has_jump")
                    and not (port.get("loss_db") or "")
                )


def _apply_store_occupancy_fallback(routes: list[dict[str, Any]]) -> None:
    """Fill occupancy only for ports the NetBox index could not decide (no front-port id)."""
    try:
        store = load_store()
    except Exception:
        return
    occ: dict[tuple[str, str, str], str] = {}
    for cid, segs in (store or {}).items():
        if not cid:
            continue
        for seg in segs or []:
            cab_seg = (seg.get("cable") or "").strip()
            pairs = (
                (seg.get("dev_a"), seg.get("port_a"), (seg.get("cab_a") or cab_seg).strip()),
                (seg.get("dev_b"), seg.get("port_b"), (seg.get("cab_b") or cab_seg).strip()),
            )
            for dev, port_name, cab in pairs:
                dev = (dev or "").strip()
                port_name = (port_name or "").strip()
                if not dev or not port_name:
                    continue
                try:
                    port_name = normalize_fiber_label(port_name)
                except Exception:
                    pass
                for cab_var in cable_label_variants(cab) or [cab]:
                    if cab_var:
                        occ[(dev, cab_var, port_name)] = cid
    if not occ:
        return
    for route in routes:
        odf = (route.get("odf") or "").strip()
        for sec in route.get("cables") or []:
            cable = (sec.get("cable") or "").strip()
            cab_keys = [c for c in (cable_label_variants(cable) or [cable]) if c]
            for port in sec.get("ports") or []:
                if port.get("in_use") or port.get("id"):
                    continue
                fiber = (port.get("fiber") or "").strip()
                name = (port.get("name") or "").strip()
                try:
                    fiber_n = normalize_fiber_label(fiber) if fiber else fiber
                except Exception:
                    fiber_n = fiber
                cid = ""
                for cab_var in cab_keys:
                    cid = occ.get((odf, cab_var, fiber_n)) or occ.get((odf, cab_var, fiber)) or occ.get((odf, cab_var, name))
                    if cid:
                        break
                if not cid:
                    continue
                port["in_use"] = True
                port["cid"] = cid
                port["empty"] = False
                port["local_summary"] = cid


def get_location_tree(client, *, force_refresh: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force_refresh and _TREE_CACHE["data"] and now - float(_TREE_CACHE["ts"]) < _TREE_TTL_SEC:
        return _TREE_CACHE["data"]

    if not force_refresh:
        disk_tree = load_tree_from_disk()
        if disk_tree is not None:
            _TREE_CACHE["ts"] = now
            _TREE_CACHE["data"] = disk_tree
            return disk_tree

    if force_refresh:
        invalidate_odf_records_cache()
    records = list_all_odf_records(client, force_refresh=force_refresh)
    rear_by_dev = _rear_ports_by_device_id(client)
    sites: dict[str, Any] = {}
    for r in records:
        st = sites.setdefault(r["site"], {"name": r["site"], "rooms": {}})
        rm = st["rooms"].setdefault(r["room"], {"name": r["room"], "records": []})
        rm["records"].append(r)
    site_list = []
    route_count = 0
    for site_name, site_data in sorted(sites.items()):
        rooms = []
        for room_name, room_data in sorted(site_data["rooms"].items()):
            routes = build_odf_route_entries(
                client, room_data["records"], rear_by_dev=rear_by_dev, light=True
            )
            if not routes:
                continue
            route_count += len(routes)
            rooms.append({"name": room_name, "odfs": routes})
        if not rooms:
            continue
        site_list.append({"name": site_name, "rooms": rooms})
    result = {"sites": site_list, "odf_count": len(records), "route_count": route_count}
    _TREE_CACHE["ts"] = now
    _TREE_CACHE["data"] = result
    save_tree_to_disk(result)
    return result


def _strand_loss_map(client, cable_label: str) -> dict[str, dict[str, str]]:
    """fiber label -> {loss_db, wavelength}."""
    cable_label = (cable_label or "").strip()
    if not cable_label:
        return {}
    cache = getattr(_strand_loss_map, "_cache", {})
    if cable_label in cache:
        return cache[cable_label]
    out: dict[str, dict[str, str]] = {}
    fc_id = None
    variants = cable_label_variants(cable_label) or [cable_label]
    for variant in variants:
        hits = client.request("GET", "/dcim/cables/", params={"label": variant, "limit": 10})
        for cab in hits.get("results") or []:
            if (cab.get("label") or "") != variant:
                continue
            fc = client.request(
                "GET",
                "/plugins/fms/fiber-cables/",
                params={"cable_id": cab["id"], "limit": 1},
            )
            if fc.get("results"):
                fc_id = fc["results"][0]["id"]
                break
        if fc_id:
            break
    if fc_id:
        for s in paginate(client, "/plugins/fms/fiber-strands/", {"fiber_cable_id": fc_id}):
            pos = s.get("position")
            fiber = fiber_from_port_name(s.get("name") or "", cable_label) or (f"1-{pos}" if pos else "")
            cf = s.get("custom_fields") or {}
            loss = cf.get("measured_loss_db")
            wl = cf.get("test_wavelength_nm") or "1310"
            if fiber and loss is not None and str(loss) != "":
                out[fiber] = {"loss_db": str(loss), "wavelength": str(wl)}
            elif pos:
                out.setdefault(f"1-{pos}", {"loss_db": "", "wavelength": str(wl)})
    for variant in variants:
        cache[variant] = out
    cache[cable_label] = out
    _strand_loss_map._cache = cache
    return out


def _fiber_label_from_port(
    name: str,
    cable_label: str = "",
    device_cables: list[str] | None = None,
    position: int | None = None,
    *,
    local_room: str = "",
) -> str:
    from naming_rules import fiber_from_port_name, fiber_from_position

    name = (name or "").strip()
    if not name and not position:
        return ""
    if cable_label:
        fb = fiber_from_port_name(name, cable_label, local_room=local_room)
        if fb:
            return fb
    for cab in device_cables or []:
        if cab == cable_label:
            continue
        fb = fiber_from_port_name(name, cab, local_room=local_room)
        if fb:
            return fb
    fb = fiber_from_port_name(name, local_room=local_room)
    if fb:
        return fb
    if position and int(position) > 0:
        return fiber_from_position(int(position))
    return name


def get_odf_modal_panel(
    client,
    odf_name: str,
    web_base: str,
    cable_filter: str,
    *,
    fp_index: dict | None = None,
) -> dict[str, Any]:
    """Fast single-route panel for port modal (no per-cable loss scan / full context)."""
    dev = find_device(client, odf_name)
    did = dev["id"]
    parsed = parse_odf_name(odf_name)
    loc = dev.get("location") or {}
    room_name = (loc.get("name", "") if isinstance(loc, dict) else "") or parsed["room"]
    site = (dev.get("site") or {}).get("name", "")

    fp_index = fp_index or build_fp_circuit_index(client, web_base)
    rear_by_dev = _rear_ports_by_device_id(client)
    rec = {
        "id": str(did),
        "name": odf_name,
        "site": site,
        "room": room_name,
        "url": f"/dcim/devices/{did}/",
    }
    entries = build_odf_route_entries(client, [rec], rear_by_dev=rear_by_dev, light=True)
    entry = next((e for e in entries if e.get("cable") == cable_filter), None)
    if not entry and entries:
        from naming_rules import cable_label_variants

        variants = set(cable_label_variants(cable_filter))
        entry = next((e for e in entries if e.get("cable") in variants), entries[0])
    if not entry:
        entry = {
            "name": odf_name,
            "display_name": odf_name,
            "cable": cable_filter,
            "peer_room": "",
            "id": str(did),
            "route_key": f"{odf_name}|{cable_filter}",
        }

    fps = paginate(client, "/dcim/front-ports/", {"device_id": did})
    fp_by_id = {fp["id"]: fp for fp in fps}
    rear_ports = paginate(client, "/dcim/rear-ports/", {"device_id": did})
    grid = _build_route_port_grid(
        client,
        entry,
        fp_by_id,
        web_base=web_base,
        device_meta=rec,
        rear_ports=rear_ports,
        fps_by_rear=_fps_by_rear_for_device(fps),
        rear_by_dev=rear_by_dev,
        fp_index=fp_index,
    )
    display_name = grid.get("display_name") or odf_name
    return {
        "odf": odf_name,
        "display_name": display_name,
        "route_key": grid.get("route_key") or entry.get("route_key"),
        "cable_filter": cable_filter,
        "site": site,
        "room": room_name,
        "cabinet": parsed["cabinet"],
        "odf_no": parsed["odf_no"],
        "device_url": grid.get("device_url") or f"{web_base.rstrip('/')}/dcim/devices/{did}/",
        "device_id": int(grid.get("device_id") or did),
        "cables": grid.get("cables") or [],
        "fibers": grid.get("fibers") or [],
        "neighbors": [],
        "peer_options": [],
        "circuit_options": [],
        "roles": [
            {"value": "a", "label": "本框为 A 端（起点侧）"},
            {"value": "b", "label": "本框为 B 端（终点侧）"},
        ],
        "wavelengths": ["1310", "1550"],
        "local_odf_options": [],
        "jump_target_options": [],
        "room_devices": [],
        "site_devices": [],
        "local_types": [
            {"value": "trunk_only", "label": "仅登记缆段（中间跳/暂无延伸）"},
            {"value": "jump_odf", "label": "跳纤到其他缆段/ODF框"},
            {"value": "to_device", "label": "直连交换机/光模块"},
        ],
        "modal": True,
        "has_context": False,
    }


def get_odf_port_panel(
    client,
    odf_name: str,
    web_base: str,
    *,
    cable_filter: str = "",
    include_context: bool = True,
    light: bool = False,
    fp_index: dict | None = None,
    rear_by_dev: dict[int, list[str]] | None = None,
) -> dict[str, Any]:
    dev = find_device(client, odf_name)
    did = dev["id"]
    parsed = parse_odf_name(odf_name)
    loc = dev.get("location") or {}
    room_name = (loc.get("name", "") if isinstance(loc, dict) else "") or parsed["room"]
    site = (dev.get("site") or {}).get("name", "")

    fp_index = fp_index or build_fp_circuit_index(client, web_base)
    rb = rear_by_dev if rear_by_dev is not None else _rear_ports_by_device_id(client)
    cables_at = {c["label"]: c.get("peer", "") for c in cables_at_odf(client, odf_name)}

    fps = paginate(client, "/dcim/front-ports/", {"device_id": did})
    fp_by_id = {fp["id"]: fp for fp in fps}
    fps_by_rear: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for fp in fps:
        rp = fp.get("rear_port") or {}
        rp_id = rp.get("id") if isinstance(rp, dict) else rp
        if rp_id:
            fps_by_rear[int(rp_id)].append(fp)

    def circuits_for_fp(fp_id: int | None) -> list[dict[str, Any]]:
        if not fp_id:
            return []
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for circ in fp_index.get(fp_id, []):
            key = f"{circ.get('cid')}|{circ.get('path_id')}"
            if key in seen:
                continue
            seen.add(key)
            out.append(circ)
        return out

    rear_ports = paginate(client, "/dcim/rear-ports/", {"device_id": did})
    device_cables = [
        (rp.get("name") or "").strip()
        for rp in rear_ports
        if is_trunk_cable_label((rp.get("name") or "").strip())
    ]
    cables_sections: list[dict[str, Any]] = []

    for rp in sorted(rear_ports, key=lambda x: x.get("name", "")):
        cable_label = rp.get("name") or ""
        if not is_trunk_cable_label(cable_label):
            continue
        display_cable = _display_cable_label(cable_label, room_name)
        loss_by_fiber = _strand_loss_map(client, cable_label)
        peer = cables_at.get(cable_label) or resolve_peer_from_cable(client, odf_name, cable_label)
        cable_peer = peer
        peer_display = _peer_display_name(client, cable_peer, cable_label, rear_by_dev=rb)
        ports: list[dict[str, Any]] = []
        def _fp_sort_key(fp: dict[str, Any]) -> tuple[int, ...]:
            fiber = _fiber_label_from_port(
                fp.get("name") or "",
                display_cable or cable_label,
                device_cables,
                fp.get("position"),
                local_room=room_name,
            )
            return fiber_sort_key(fiber)

        linked_fps = sorted(fps_by_rear.get(int(rp["id"]), []), key=_fp_sort_key)
        if not linked_fps:
            rp_detail = client.request("GET", f"/dcim/rear-ports/{rp['id']}/")
            for link in rp_detail.get("front_ports") or []:
                fp_id = link.get("front_port")
                fp = fp_by_id.get(fp_id)
                if fp:
                    linked_fps.append(fp)
            linked_fps.sort(key=_fp_sort_key)
        if not linked_fps:
            fibers = default_fiber_options()
            for i, f in enumerate(fibers[:12]):
                loss_info = loss_by_fiber.get(f, {})
                ports.append(
                    {
                        "id": None,
                        "position": i + 1,
                        "name": "",
                        "fiber": f,
                        "cable": display_cable or cable_label,
                        "linked": False,
                        "peer_names": [],
                        "has_jump": False,
                        "loss_db": loss_info.get("loss_db", ""),
                        "wavelength": loss_info.get("wavelength", "1310"),
                        "empty": True,
                        "in_use": False,
                        "cid": "",
                        "circuit_name": "",
                        "is_complete": False,
                        "pending": False,
                        "trace_url": "",
                        "local_segment": "",
                        "peer_odf": cable_peer,
                        "local_type": "empty",
                        "local_summary": "",
                    }
                )
            cables_sections.append(
                {"cable": display_cable or cable_label, "peer_odf": cable_peer, "peer_display_name": peer_display, "ports": ports}
            )
            continue
        for fp in linked_fps:
            fp_id = fp["id"]
            name = fp.get("name") or f"pos-{fp_id}"
            fiber = _fiber_label_from_port(
                name, display_cable or cable_label, device_cables, fp.get("position"), local_room=room_name
            )
            peers = fp.get("link_peers") or []
            linked = len(peers) > 0
            peer_names = [p.get("name") for p in peers if isinstance(p, dict) and p.get("name")]
            jump_odf = ""
            jump_fiber = ""
            for link_peer in peers:
                if not isinstance(link_peer, dict):
                    continue
                dev = link_peer.get("device") or {}
                dev_name = dev.get("name", "") if isinstance(dev, dict) else ""
                if dev_name and "ODF" in dev_name.upper():
                    jump_odf = dev_name
                    jump_fiber = _fiber_label_from_port(link_peer.get("name") or "", "", device_cables)
                    break
            cab = fp.get("cable")
            has_jump = linked or (isinstance(cab, dict) and bool(cab.get("id")))
            loss_info = loss_by_fiber.get(fiber, {})
            circuits = circuits_for_fp(fp_id)
            in_use = len(circuits) > 0
            primary = circuits[0] if circuits else {}
            if light:
                local_seg = {"summary": f"{fiber} ↔ {peer_display} {fiber}" if peer_display else f"{fiber} ↔ {cable_peer} {fiber}"}
                if has_jump or linked:
                    tgt = jump_odf or (peer_names[0] if peer_names else "")
                    pf = jump_fiber or fiber
                    local = {"local_type": "jump", "summary": f"跳纤 → {tgt} {pf}".strip()}
                else:
                    local = {"local_type": "empty", "summary": "空闲（仅缆段侧）"}
            else:
                local_seg = local_segment_at_odf(odf_name, cable_label, fiber, cable_peer, client=client)
                local = describe_front_port_local_from_fp(
                    fp, client=client, odf_name=odf_name, cable_label=cable_label
                )
            empty = not in_use and not linked and not has_jump and not loss_info.get("loss_db")
            ports.append(
                {
                    "id": fp_id,
                    "position": fp.get("position") or 0,
                    "name": name,
                    "fiber": fiber,
                    "cable": display_cable or cable_label,
                    "linked": linked,
                    "peer_names": peer_names,
                    "jump_odf": jump_odf,
                    "jump_fiber": jump_fiber,
                    "has_jump": has_jump,
                    "loss_db": loss_info.get("loss_db", ""),
                    "wavelength": loss_info.get("wavelength", "1310"),
                    "empty": empty,
                    "in_use": in_use,
                    "cid": primary.get("cid", ""),
                    "circuit_name": primary.get("name", ""),
                    "is_complete": primary.get("is_complete", False),
                    "pending": primary.get("pending", False),
                    "trace_url": primary.get("trace_url", ""),
                    "local_segment": local_seg.get("summary", ""),
                    "peer_odf": cable_peer,
                    "local_type": local.get("local_type", "empty"),
                    "local_summary": local.get("summary", ""),
                }
            )
        ports.sort(key=lambda x: fiber_sort_key(x.get("fiber") or ""))
        cables_sections.append(
            {
                "cable": display_cable or cable_label,
                "peer_odf": cable_peer,
                "peer_display_name": peer_display,
                "ports": ports,
            }
        )

    if not cables_sections:
        for cab in cables_at_odf(client, odf_name):
            fibers = default_fiber_options()
            loss_by_fiber = _strand_loss_map(client, cab["label"])
            ports = [
                {
                    "id": None,
                    "position": i + 1,
                    "name": "",
                    "fiber": f,
                    "cable": cab["label"],
                    "linked": False,
                    "peer_names": [],
                    "has_jump": False,
                    "loss_db": loss_by_fiber.get(f, {}).get("loss_db", ""),
                    "wavelength": loss_by_fiber.get(f, {}).get("wavelength", "1310"),
                    "empty": True,
                }
                for i, f in enumerate(fibers[:12])
            ]
            peer = cab.get("peer", "")
            cables_sections.append(
                {
                    "cable": cab["label"],
                    "peer_odf": peer,
                    "peer_display_name": _peer_display_name(client, peer, cab["label"], rear_by_dev=rb),
                    "ports": ports,
                }
            )

    if cable_filter:
        cables_sections = _sections_for_cable_filter(cables_sections, cable_filter)

    display_name, route_key = _display_name_for_route(
        client, odf_name, room_name, did, cable_filter, rear_by_dev=rb
    )

    if include_context:
        from field_service import get_odf_context

        ctx = get_odf_context(client, odf_name, web_base, fp_index=fp_index, cable_filter=cable_filter)
    else:
        ctx = {
            "neighbors": [],
            "peer_options": [],
            "circuit_options": [],
            "fibers": list({p["fiber"] for s in cables_sections for p in s["ports"] if p.get("fiber")}),
            "roles": [],
            "wavelengths": ["1310", "1550"],
            "local_odf_options": [],
            "jump_target_options": [],
            "room_devices": [],
            "site_devices": [],
            "local_types": [],
        }

    return {
        "odf": odf_name,
        "display_name": display_name,
        "route_key": route_key,
        "cable_filter": cable_filter,
        "site": site,
        "room": room_name,
        "cabinet": parsed["cabinet"],
        "odf_no": parsed["odf_no"],
        "device_url": f"{web_base.rstrip('/')}/dcim/devices/{did}/",
        "device_id": did,
        "cables": cables_sections,
        "neighbors": ctx.get("neighbors", []),
        "peer_options": ctx.get("peer_options", []),
        "circuit_options": ctx.get("circuit_options", []),
        "fibers": ctx.get("fibers", []) if include_context else sorted(
            {p["fiber"] for s in cables_sections for p in s["ports"] if p.get("fiber")}
        ),
        "roles": ctx.get("roles", []),
        "wavelengths": ctx.get("wavelengths", ["1310", "1550"]),
        "local_odf_options": ctx.get("local_odf_options", []),
        "jump_target_options": ctx.get("jump_target_options", []),
        "room_devices": ctx.get("room_devices", []),
        "site_devices": ctx.get("site_devices", []),
        "local_types": ctx.get("local_types", []),
        "has_context": include_context,
    }


def _attach_panel_meta(
    panel: dict[str, Any],
    *,
    cached: bool,
    stale: bool,
    age_sec: float,
    occupancy_loaded: bool = True,
) -> dict[str, Any]:
    out = dict(panel)
    out["_meta"] = {
        "cached": cached,
        "stale": stale,
        "age_sec": int(max(0, age_sec)),
        "occupancy_loaded": occupancy_loaded,
    }
    return out


def _panel_occupancy_loaded(panel: dict[str, Any] | None) -> bool:
    """True only when occupancy was explicitly merged — not merely 'some ports look used'."""
    if not panel:
        return False
    if panel.get("occupancy_loaded") is True:
        return True
    meta = panel.get("_meta") or {}
    return meta.get("occupancy_loaded") is True


def _read_cached_panel(
    cache_key: str,
    site_name: str,
    room_name: str,
    *,
    allow_stale_disk: bool = False,
) -> tuple[dict[str, Any] | None, float]:
    mem = _ROOM_PANEL_CACHE.get(cache_key)
    if mem and mem.get("data"):
        return dict(mem["data"]), float(mem.get("ts") or 0)
    disk_panel, disk_ts = load_room_panel_entry(site_name, room_name, allow_stale=allow_stale_disk)
    if disk_panel is not None:
        ts = float(disk_ts or 0)
        _ROOM_PANEL_CACHE[cache_key] = {"ts": ts or time.time(), "data": disk_panel}
        return dict(disk_panel), ts
    return None, 0.0


def _store_room_panel_cache(
    cache_key: str,
    result: dict[str, Any],
    *,
    site_name: str,
    room_name: str,
    occupancy_loaded: bool | None = None,
) -> None:
    payload = {k: v for k, v in result.items() if k != "_meta"}
    if occupancy_loaded is None:
        occupancy_loaded = _panel_occupancy_loaded(payload)
    payload["occupancy_loaded"] = bool(occupancy_loaded)
    existing = _ROOM_PANEL_CACHE.get(cache_key)
    if existing and existing.get("data"):
        old = existing["data"]
        if _panel_occupancy_loaded(old) and not payload["occupancy_loaded"]:
            return
    now = time.time()
    _ROOM_PANEL_CACHE[cache_key] = {"ts": now, "data": payload}
    if payload["occupancy_loaded"]:
        save_room_panel_to_disk(site_name, room_name, payload)


def _schedule_room_panel_refresh(client, site_name: str, room_name: str, web_base: str, cache_key: str) -> None:
    with _ROOM_REFRESH_LOCK:
        if cache_key in _ROOM_REFRESH_INFLIGHT:
            return
        _ROOM_REFRESH_INFLIGHT.add(cache_key)

    def _run() -> None:
        try:
            get_room_port_panel(
                client, site_name, room_name, web_base,
                occupancy_only=True, force_refresh=True,
            )
        except Exception as exc:
            sys.stderr.write(f"[cache] background room refresh {cache_key} failed: {exc}\n")
        finally:
            with _ROOM_REFRESH_LOCK:
                _ROOM_REFRESH_INFLIGHT.discard(cache_key)

    threading.Thread(target=_run, daemon=True, name=f"room-refresh-{cache_key}").start()


def _build_room_port_panel(
    client,
    site_name: str,
    room_name: str,
    web_base: str,
    *,
    include_occupancy: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Build room panel from NetBox (no cache read)."""
    records = [r for r in list_all_odf_records(client) if r["site"] == site_name and r["room"] == room_name]
    if not records:
        return {
            "site": site_name,
            "room": room_name,
            "routes": [],
            "route_count": 0,
            "odfs": [],
            "odf_count": 0,
            "route_order": [],
        }

    rear_by_dev = _rear_ports_by_device_id(client)
    entries = build_odf_route_entries(client, records, rear_by_dev=rear_by_dev, light=True)

    rec_by_id = {int(r["id"]): r for r in records if str(r.get("id", "")).isdigit()}
    device_ids = set(rec_by_id.keys())
    fp_by_device: dict[int, dict[int, dict[str, Any]]] = {}
    fps_by_rear_by_dev: dict[int, dict[int, list[dict[str, Any]]]] = {}
    rear_ports_by_dev: dict[int, list[dict[str, Any]]] = {}
    target_fp_ids: set[int] = set()
    for did in device_ids:
        fps = paginate(client, "/dcim/front-ports/", {"device_id": did})
        fp_by_device[did] = {fp["id"]: fp for fp in fps}
        target_fp_ids.update(fp_by_device[did].keys())
        fps_by_rear_by_dev[did] = _fps_by_rear_for_device(fps)
        rear_ports_by_dev[did] = paginate(client, "/dcim/rear-ports/", {"device_id": did})

    fp_index: dict[int, list[dict[str, Any]]] = {}
    if include_occupancy and target_fp_ids:
        fp_index = fp_index_for_targets(client, web_base, target_fp_ids, force_refresh=force_refresh)

    routes = []
    for entry in entries:
        did = int(entry["id"]) if str(entry.get("id", "")).isdigit() else 0
        routes.append(
            _build_route_port_grid(
                client,
                entry,
                fp_by_device.get(did, {}),
                web_base=web_base,
                device_meta=rec_by_id.get(did),
                rear_ports=rear_ports_by_dev.get(did, []),
                fps_by_rear=fps_by_rear_by_dev.get(did, {}),
                rear_by_dev=rear_by_dev,
                fp_index=fp_index if include_occupancy else {},
                include_loss=include_occupancy,
            )
        )

    from room_layout import get_room_order, sort_routes_by_layout

    routes = sort_routes_by_layout(routes, site_name, room_name)
    if include_occupancy:
        _apply_store_occupancy_fallback(routes)
    return {
        "site": site_name,
        "room": room_name,
        "routes": routes,
        "route_count": len(routes),
        "odfs": routes,
        "odf_count": len(routes),
        "route_order": get_room_order(site_name, room_name),
        "overview": True,
        "occupancy_loaded": include_occupancy,
    }


def _merge_occupancy_into_panel(
    client,
    panel: dict[str, Any],
    web_base: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    routes = panel.get("routes") or []
    fp_ids = _collect_panel_fp_ids(routes)
    if fp_ids:
        fp_index = fp_index_for_targets(client, web_base, fp_ids, force_refresh=force_refresh)
        _apply_fp_index_to_routes(routes, fp_index)
    _apply_store_occupancy_fallback(routes)
    try:
        _apply_loss_to_routes(client, routes)
    except Exception as exc:
        sys.stderr.write(f"[cache] loss merge failed: {exc}\n")
    panel["routes"] = routes
    panel["odfs"] = routes
    panel["occupancy_loaded"] = True
    return panel


def get_room_port_panel(
    client,
    site_name: str,
    room_name: str,
    web_base: str,
    *,
    force_refresh: bool = False,
    fast: bool = False,
    occupancy_only: bool = False,
) -> dict[str, Any]:
    """Room ODF panels. Prefer last occupancy snapshot; never clobber it with a skeleton."""
    cache_key = _room_panel_cache_key(site_name, room_name)
    now = time.time()

    if occupancy_only:
        if force_refresh:
            result = _build_room_port_panel(
                client, site_name, room_name, web_base, include_occupancy=True, force_refresh=True
            )
            result["occupancy_loaded"] = True
            _store_room_panel_cache(
                cache_key, result, site_name=site_name, room_name=room_name, occupancy_loaded=True
            )
            return _attach_panel_meta(
                result, cached=False, stale=False, age_sec=0, occupancy_loaded=True
            )
        cached, _ts = _read_cached_panel(cache_key, site_name, room_name, allow_stale_disk=True)
        cached_ids = _collect_panel_fp_ids((cached or {}).get("routes") or [])
        if cached and cached.get("routes") and cached_ids:
            structure = dict(cached)
        else:
            structure = _build_room_port_panel(
                client, site_name, room_name, web_base, include_occupancy=False, force_refresh=False
            )
        panel = _merge_occupancy_into_panel(
            client, structure, web_base, force_refresh=force_refresh
        )
        _store_room_panel_cache(
            cache_key, panel, site_name=site_name, room_name=room_name, occupancy_loaded=True
        )
        return _attach_panel_meta(
            panel, cached=not force_refresh, stale=False, age_sec=0, occupancy_loaded=True
        )

    if fast:
        cached, ts = _read_cached_panel(cache_key, site_name, room_name, allow_stale_disk=True)
        if cached and cached.get("routes"):
            occ = _panel_occupancy_loaded(cached)
            return _attach_panel_meta(
                cached,
                cached=True,
                stale=now - ts > _ROOM_PANEL_TTL_SEC if ts else False,
                age_sec=now - ts if ts else 0,
                occupancy_loaded=occ,
            )
        # Cache miss: return structure only. Occupancy is filled by the follow-up
        # occupancy=1 request. Waiting for occupancy here blocks the first paint
        # (and the "加载机房 ODF 框…" spinner) for every uncached room.
        result = _build_room_port_panel(
            client, site_name, room_name, web_base, include_occupancy=False, force_refresh=False
        )
        result["occupancy_loaded"] = False
        return _attach_panel_meta(result, cached=False, stale=False, age_sec=0, occupancy_loaded=False)

    if force_refresh:
        result = _build_room_port_panel(
            client, site_name, room_name, web_base, include_occupancy=True, force_refresh=True
        )
        result["occupancy_loaded"] = True
        _store_room_panel_cache(
            cache_key, result, site_name=site_name, room_name=room_name, occupancy_loaded=True
        )
        return _attach_panel_meta(result, cached=False, stale=False, age_sec=0, occupancy_loaded=True)

    mem = _ROOM_PANEL_CACHE.get(cache_key)
    if mem and now - float(mem.get("ts", 0)) < _ROOM_PANEL_TTL_SEC:
        data = mem["data"]
        return _attach_panel_meta(
            data,
            cached=True,
            stale=False,
            age_sec=now - float(mem["ts"]),
            occupancy_loaded=_panel_occupancy_loaded(data),
        )

    disk_panel, disk_ts = load_room_panel_entry(site_name, room_name, allow_stale=True)
    if disk_panel is not None and disk_ts is not None:
        age = now - disk_ts
        occ = _panel_occupancy_loaded(disk_panel)
        _ROOM_PANEL_CACHE[cache_key] = {"ts": disk_ts, "data": disk_panel}
        if age < _ROOM_PANEL_TTL_SEC:
            return _attach_panel_meta(
                disk_panel, cached=True, stale=False, age_sec=age, occupancy_loaded=occ
            )
        _schedule_room_panel_refresh(client, site_name, room_name, web_base, cache_key)
        return _attach_panel_meta(
            disk_panel, cached=True, stale=True, age_sec=age, occupancy_loaded=occ
        )

    result = _build_room_port_panel(
        client, site_name, room_name, web_base, include_occupancy=True, force_refresh=False
    )
    result["occupancy_loaded"] = True
    _store_room_panel_cache(
        cache_key, result, site_name=site_name, room_name=room_name, occupancy_loaded=True
    )
    return _attach_panel_meta(result, cached=False, stale=False, age_sec=0, occupancy_loaded=True)


def list_tree_rooms(tree: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    """(site, room) pairs from the location tree (ODF rooms only)."""
    if tree is None:
        tree = _TREE_CACHE.get("data") or load_tree_from_disk() or {}
    out: list[tuple[str, str]] = []
    for site in tree.get("sites") or []:
        site_name = (site.get("name") or "").strip()
        for room in site.get("rooms") or []:
            room_name = (room.get("name") or "").strip()
            if site_name and room_name:
                out.append((site_name, room_name))
    return out


def prime_room_panels_from_disk() -> int:
    """Load disk snapshots into memory with a fresh timestamp (no NetBox, no bg refresh)."""
    now = time.time()
    ok = 0
    for key in list_warmed_room_keys():
        if "|" not in key:
            continue
        site, room = key.split("|", 1)
        data, _ts = load_room_panel_entry(site, room, allow_stale=True)
        if not data or not data.get("routes"):
            continue
        cache_key = _room_panel_cache_key(site, room)
        payload = dict(data)
        _ROOM_PANEL_CACHE[cache_key] = {"ts": now, "data": payload}
        ok += 1
    return ok


def room_panel_on_disk(site_name: str, room_name: str) -> bool:
    data, _ts = load_room_panel_entry(site_name, room_name, allow_stale=True)
    return bool(data and data.get("routes"))

