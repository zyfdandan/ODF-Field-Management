#!/usr/bin/env python3
"""Field registration service: ODF QR scan -> segment/loss -> NetBox trace."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fiber_infra import (
    FiberInfra,
    build_fiber_chain_from_trace,
    cross_fiber_splices_from_segments,
    infer_links_from_segments,
    jump_links_from_segments,
    merge_link_rows,
    resolve_port_name,
    sort_links_for_apply,
)
from field_cache import (
    FP_INDEX_MEM_TTL_SEC,
    get_fms_path_trace,
    invalidate_fp_index_disk,
    load_fp_index_from_disk,
    save_fp_index_to_disk,
    warm_trace_cache,
)
from import_from_excel import (
    NetBoxClient,
    build_odf_label_manifest,
    build_route_short,
    build_table_description,
    find_device,
    find_front_port,
    group_by_circuit,
    merge_circuit_meta,
    validate_chain,
    _path_optical_fields,
)
from unified_sheet import resolve_cable_for_odf_loss
from naming_rules import (
    _rooms_match,
    cable_label_variants,
    cable_port_prefix,
    fiber_from_port_name,
    fiber_sort_key,
    format_odf_device_name,
    is_trunk_cable_label,
    local_room_from_odf_name,
    mirror_cable_label,
    normalize_room_name,
    parse_route_label,
    peer_room_from_odf_name,
)

STORE_PATH = Path(__file__).with_name("circuit_store.json")
STORE_LOCK_PATH = Path(__file__).with_name("circuit_store.json.lock")
_STORE_THREAD_LOCK = threading.RLock()
_ODF_NAME_RE = re.compile(r"^(.+?)-([A-Za-z0-9]+)-ODF-(\d+)$")
_CONFLICT_HINT = "请刷新页面后重试"


def _safe_path_id(path_id: Any) -> int | None:
    """Return a positive int path id, or None when missing/invalid."""
    if path_id is None or path_id is False:
        return None
    if isinstance(path_id, str):
        text = path_id.strip()
        if not text or text.lower() in ("none", "null", "undefined"):
            return None
        if not text.isdigit():
            return None
        path_id = int(text)
    try:
        value = int(path_id)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _trace_url_for_path(web_base: str, path_id: Any) -> str:
    """Build NetBox path trace URL only when path_id is a real int (never .../None)."""
    pid = _safe_path_id(path_id)
    if not pid:
        return ""
    base = (web_base or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/plugins/fms/fiber-circuit-paths/{pid}/#trace"


class _StoreFileLock:
    """Cross-process exclusive lock for circuit_store.json (fcntl / msvcrt)."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        if os.name == "nt":
            import msvcrt

            self._fh.seek(0)
            while True:
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        return False


def _read_store_unlocked() -> dict[str, list[dict[str, str]]]:
    if not STORE_PATH.exists():
        return {}
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_store_unlocked(data: dict[str, list[dict[str, str]]]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(STORE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STORE_PATH)


def _room_from_odf_name(odf_name: str) -> str:
    m = _ODF_NAME_RE.match((odf_name or "").strip())
    return m.group(1) if m else ""


def _fp_id(obj: Any) -> int | None:
    if isinstance(obj, dict) and obj.get("id"):
        return int(obj["id"])
    if obj not in (None, ""):
        try:
            return int(obj)
        except (TypeError, ValueError):
            return None
    return None


def extract_business_ip(text: str) -> str:
    m = re.search(r"业务IP:\s*(\S+)", text or "")
    if m and m.group(1) not in ("-", "—"):
        return m.group(1)
    return ""


def fp_ids_on_fms_path(path: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    for key in ("origin", "destination"):
        pid = _fp_id(path.get(key))
        if pid:
            ids.add(pid)
    for hop in path.get("path") or []:
        if isinstance(hop, dict) and hop.get("type") == "front_port":
            pid = _fp_id(hop.get("id"))
            if pid:
                ids.add(pid)
    return ids


def invalidate_fp_circuit_index_cache() -> None:
    if hasattr(build_fp_circuit_index, "_cache"):
        delattr(build_fp_circuit_index, "_cache")
    invalidate_fp_index_disk()


def invalidate_after_circuit_change(
    path_id: int | None = None,
    *,
    wipe_fp_index: bool = True,
) -> None:
    """Clear occupancy index and trace cache after portal submit/retrace.

    Submit patches the occupancy index in place (wipe_fp_index=False) so the
    UI does not wait for a full FMS path pagination.
    """
    if wipe_fp_index:
        invalidate_fp_circuit_index_cache()
    invalidate_trace_display_caches()
    if path_id is not None:
        from field_cache import invalidate_trace_cache

        invalidate_trace_cache(int(path_id))
    else:
        from field_cache import invalidate_trace_cache

        invalidate_trace_cache()


def _fp_index_lock() -> threading.Lock:
    lock = getattr(build_fp_circuit_index, "_lock", None)
    if lock is None:
        lock = threading.Lock()
        build_fp_circuit_index._lock = lock
    return lock


def _fp_index_rebuild_lock() -> threading.Lock:
    lock = getattr(build_fp_circuit_index, "_rebuild_lock", None)
    if lock is None:
        lock = threading.Lock()
        build_fp_circuit_index._rebuild_lock = lock
    return lock


def upsert_fp_index_entries(
    web_base: str,
    fp_ids: set[int],
    info: dict[str, Any],
) -> None:
    """Patch complete occupancy index for just-created/updated circuit ports."""
    web_base = (web_base or "").rstrip("/")
    fp_ids = {int(x) for x in fp_ids if x}
    if not web_base or not fp_ids:
        return
    cid = (info.get("cid") or "").strip()
    path_id = info.get("path_id")
    with _fp_index_lock():
        cache = getattr(build_fp_circuit_index, "_cache", None)
        data: dict[int, list[dict[str, Any]]] | None = None
        complete = False
        if cache and cache.get("web_base") == web_base:
            data = dict(cache.get("data") or {})
            complete = bool(cache.get("complete"))
        else:
            disk = load_fp_index_from_disk(web_base)
            if disk is not None:
                data = dict(disk)
                complete = True
        if data is None:
            return
        entry = dict(info)
        for fp_id in fp_ids:
            existing = [
                x
                for x in (data.get(fp_id) or [])
                if (x.get("cid") or "").strip() != cid and x.get("path_id") != path_id
            ]
            existing.insert(0, dict(entry))
            data[fp_id] = existing
        build_fp_circuit_index._cache = {
            "ts": time.time(),
            "web_base": web_base,
            "data": data,
            "complete": complete,
        }
        if complete:
            try:
                save_fp_index_to_disk(web_base, data)
            except Exception:
                pass


def _fp_ids_from_path_nodes(
    path_nodes: list[dict[str, Any]] | None,
    origin_id: Any = None,
    dest_id: Any = None,
) -> set[int]:
    ids: set[int] = set()
    for node in path_nodes or []:
        if isinstance(node, dict) and node.get("type") == "front_port":
            pid = _fp_id(node.get("id"))
            if pid:
                ids.add(pid)
    for raw in (origin_id, dest_id):
        pid = _fp_id(raw)
        if pid:
            ids.add(pid)
    return ids


def _finalize_path_in_background(
    config: dict[str, Any],
    path_id: int,
    segs: list[dict[str, str]],
    provisional_complete: bool,
) -> None:
    """Trace + fiber_chain after HTTP response; path write already succeeded."""

    def _run() -> None:
        try:
            client = client_from_config(config)
            web_base = config.get("web_base_url", config["base_url"].replace("/api", ""))
            trace = get_fms_path_trace(client, int(path_id), web_base, force_refresh=True)
            ok = _registered_endpoints_in_trace(trace, segs, client)
            if ok != provisional_complete:
                client.request(
                    "PATCH",
                    f"/plugins/fms/fiber-circuit-paths/{path_id}/",
                    json={"is_complete": ok},
                )
            chain, _ = build_fiber_chain_from_trace(trace)
            if chain:
                FiberInfra(client).apply_fiber_chain(int(path_id), trace=trace)
        except Exception as exc:
            print(f"[path-finalize {path_id}] {exc}")

    threading.Thread(
        target=_run, daemon=True, name=f"path-finalize-{path_id}"
    ).start()


_DEVICE_MEM: dict[str, tuple[float, dict[str, Any]]] = {}
_DEVICE_MEM_TTL_SEC = 3600
_CABLE_FOR_PORT_MEM: dict[str, str] = {}
_ROUTE_DISP_MEM: dict[str, str] = {}
_PORT_DETAIL_MEM: dict[str, dict[str, Any]] = {}
_PORT_DETAIL_TTL_SEC = 3600
_PEER_RESOLVE_MEM: dict[str, tuple[float, str]] = {}
_PEER_RESOLVE_TTL_SEC = 3600
_DISPLAY_CACHE_LOCK = threading.Lock()


def invalidate_trace_display_caches() -> None:
    """Drop hop-label and port-detail caches (keep FMS trace JSON)."""
    with _DISPLAY_CACHE_LOCK:
        _DEVICE_MEM.clear()
        _CABLE_FOR_PORT_MEM.clear()
        _ROUTE_DISP_MEM.clear()
        _PORT_DETAIL_MEM.clear()
        _PEER_RESOLVE_MEM.clear()


def _cached_find_device(client: NetBoxClient, name: str) -> dict[str, Any]:
    name = (name or "").strip()
    now = time.time()
    with _DISPLAY_CACHE_LOCK:
        hit = _DEVICE_MEM.get(name)
        if hit and now - hit[0] < _DEVICE_MEM_TTL_SEC:
            return hit[1]
    dev = find_device(client, name)
    with _DISPLAY_CACHE_LOCK:
        _DEVICE_MEM[name] = (now, dev)
    return dev


def invalidate_caches_for_webhook(
    target: str = "all",
    *,
    path_id: int | None = None,
    site: str = "",
    room: str = "",
) -> None:
    """Invalidate caches from webhook or admin API."""
    from field_cache import invalidate_all_caches, invalidate_room_panel_disk, invalidate_trace_cache, invalidate_tree_disk

    target = (target or "all").lower()
    if target == "all":
        invalidate_all_caches()
        try:
            from field_browser import invalidate_odf_records_cache, invalidate_room_panel_cache

            invalidate_odf_records_cache()
            invalidate_room_panel_cache()
        except Exception:
            pass
        try:
            from portal_settings import invalidate_tree_cache

            invalidate_tree_cache()
        except Exception:
            pass
        if hasattr(build_fp_circuit_index, "_cache"):
            delattr(build_fp_circuit_index, "_cache")
        return
    if target in ("fp_index", "circuit_index"):
        invalidate_fp_circuit_index_cache()
    elif target == "trace":
        if path_id is not None:
            invalidate_trace_cache(int(path_id))
        else:
            invalidate_trace_cache()
    elif target in ("room_panel", "room"):
        try:
            from field_browser import invalidate_room_panel_cache

            invalidate_room_panel_cache(site=site, room=room)
        except Exception:
            invalidate_room_panel_disk(site=site, room=room)
    elif target == "tree":
        invalidate_tree_disk()
        try:
            from portal_settings import invalidate_tree_cache

            invalidate_tree_cache()
        except Exception:
            pass


def _cable_label_for_fp(client: NetBoxClient, fp: dict[str, Any]) -> str:
    rp = fp.get("rear_port") or {}
    if isinstance(rp, dict) and rp.get("name"):
        return (rp["name"] or "").strip()
    rp_id = rp.get("id") if isinstance(rp, dict) else rp
    if rp_id:
        try:
            rp_obj = client.request("GET", f"/dcim/rear-ports/{rp_id}/")
            return (rp_obj.get("name") or "").strip()
        except Exception:
            pass
    dev = fp.get("device") or {}
    odf = dev.get("name", "") if isinstance(dev, dict) else ""
    if odf:
        return cable_for_port_on_odf(client, odf, fp.get("name") or "")
    return ""


def _front_port_link_peer_ids(client: NetBoxClient, fp_id: int) -> list[int]:
    fp = client.request("GET", f"/dcim/front-ports/{fp_id}/")
    ids: list[int] = []
    for peer in fp.get("link_peers") or []:
        pid = _fp_id(peer)
        if pid and pid != fp_id:
            ids.append(pid)
    cab = fp.get("cable")
    if isinstance(cab, dict) and cab.get("id"):
        try:
            cab_detail = client.request("GET", f"/dcim/cables/{cab['id']}/")
            for term in (cab_detail.get("a_terminations") or []) + (cab_detail.get("b_terminations") or []):
                obj = term.get("object") or {}
                ot = (term.get("object_type") or obj.get("url") or "").lower()
                if "frontport" not in ot and "/front-ports/" not in ot:
                    continue
                pid = obj.get("id")
                if pid and int(pid) != fp_id:
                    ids.append(int(pid))
        except Exception:
            pass
    return ids


def _peer_trunk_front_port_id(client: NetBoxClient, odf: str, cable_label: str, fiber: str) -> int | None:
    if not odf or not cable_label or not fiber:
        return None
    peer_odf = resolve_peer_from_cable(client, odf, cable_label)
    if not peer_odf or peer_odf == odf:
        return None
    cab_on_peer = cable_label_on_odf(client, peer_odf, odf, cable_label)
    try:
        dev = find_device(client, peer_odf)
        fp = find_front_port(client, dev["id"], resolve_port_name(fiber, cab_on_peer, device_name=peer_odf), fiber)
        return int(fp["id"])
    except Exception:
        return None


def _collect_segment_fp_ids(
    client: NetBoxClient,
    seg: dict[str, str],
    *,
    mirror_trunk_peer: bool = False,
) -> set[int]:
    from naming_rules import normalize_fiber_label

    ids: set[int] = set()
    cab_seg = (seg.get("cable") or "").strip()
    pairs = (
        (seg.get("dev_a"), seg.get("port_a"), (seg.get("cab_a") or cab_seg).strip()),
        (seg.get("dev_b"), seg.get("port_b"), (seg.get("cab_b") or cab_seg).strip()),
    )
    for dev, port, cab in pairs:
        if not dev or not port:
            continue
        port = normalize_fiber_label(port)
        try:
            dev_obj = find_device(client, dev)
            fp = find_front_port(client, dev_obj["id"], resolve_port_name(port, cab, device_name=dev), port)
            ids.add(int(fp["id"]))
        except Exception:
            continue
    if mirror_trunk_peer:
        db = (seg.get("dev_b") or "").strip()
        pb = normalize_fiber_label((seg.get("port_b") or "").strip())
        cab_b = (seg.get("cab_b") or cab_seg).strip()
        if db and pb and cab_b:
            peer_fp = _peer_trunk_front_port_id(client, db, cab_b, pb)
            if peer_fp:
                ids.add(int(peer_fp))
    return ids


def _is_same_odf_cross_fiber_jump(seg: dict[str, str]) -> bool:
    """Same ODF box, different fibers (e.g. 1-1→1-3 换缆跳纤) — only landing port is business占用."""
    da = (seg.get("dev_a") or "").strip()
    db = (seg.get("dev_b") or "").strip()
    pa = (seg.get("port_a") or "").strip()
    pb = (seg.get("port_b") or "").strip()
    return bool(da and da == db and pa and pb and pa != pb)


def _collect_segment_fp_ids_for_occupancy(
    client: NetBoxClient,
    seg: dict[str, str],
    *,
    mirror_trunk_peer: bool = False,
) -> set[int]:
    """Occupancy index: cross-fiber same-frame jumps count landing fiber only (not 1-1 source)."""
    if _is_same_odf_cross_fiber_jump(seg):
        ids: set[int] = set()
        db = (seg.get("dev_b") or "").strip()
        pb = (seg.get("port_b") or "").strip()
        cab_b = (seg.get("cab_b") or seg.get("cable") or "").strip()
        if db and pb:
            try:
                dev_obj = find_device(client, db)
                fp = find_front_port(client, dev_obj["id"], resolve_port_name(pb, cab_b, device_name=db), pb)
                ids.add(int(fp["id"]))
            except Exception:
                pass
        return ids
    return _collect_segment_fp_ids(client, seg, mirror_trunk_peer=mirror_trunk_peer)


def _fp_device_id(client: NetBoxClient, fp_id: int, cache: dict[int, int]) -> int:
    if fp_id in cache:
        return cache[fp_id]
    try:
        fp = client.request("GET", f"/dcim/front-ports/{fp_id}/")
        did = int((fp.get("device") or {}).get("id") or 0)
    except Exception:
        did = 0
    cache[fp_id] = did
    return did


def expand_circuit_fp_ids(client: NetBoxClient, seed_fp_ids: set[int]) -> set[int]:
    """Follow front-port jump/patch links only.

    Do not auto-map trunk ports by matching fiber numbers across ODFs — that
    assumes same-fiber pairing and wrongly marks e.g. local 1-2 when the path
    uses cross-fiber 1-3↔1-2. Trunk endpoints come from FMS path / segment rows.
    """
    expanded = set(seed_fp_ids)
    changed = True
    while changed:
        changed = False
        for fp_id in list(expanded):
            for peer_id in _front_port_link_peer_ids(client, fp_id):
                if peer_id not in expanded:
                    expanded.add(peer_id)
                    changed = True
    return expanded


def _direct_jump_peer_fp_ids(client: NetBoxClient, fp_ids: set[int]) -> set[int]:
    """One-hop jump peers — skip same-ODF source (1-1) when landing (1-3) already占用."""
    extra: set[int] = set()
    dev_cache: dict[int, int] = {}
    for fp_id in fp_ids:
        dev_id = _fp_device_id(client, int(fp_id), dev_cache)
        for peer_id in _front_port_link_peer_ids(client, int(fp_id)):
            if peer_id in fp_ids:
                continue
            if dev_id and _fp_device_id(client, int(peer_id), dev_cache) == dev_id:
                continue
            extra.add(int(peer_id))
    return extra


def occupancy_fp_ids_for_circuit(
    client: NetBoxClient,
    segs: list[dict[str, str]],
    path: dict[str, Any] | None = None,
) -> set[int]:
    """Field-panel in-use ports: segment endpoints + direct jump peers only.

    Do not mirror full FMS retrace hops — cross-fiber trunks may land on the
    wrong same-position front port (e.g. peer 1-1) while registration uses 1-2.
    Same-frame cross-fiber jumps (1-1→1-3) mark only the landing port as占用.
    """
    if segs:
        fps: set[int] = set()
        for i, seg in enumerate(segs):
            fps |= _collect_segment_fp_ids_for_occupancy(client, seg, mirror_trunk_peer=(i == len(segs) - 1))
        return fps | _direct_jump_peer_fp_ids(client, fps)
    if path:
        return fp_ids_on_fms_path(path)
    return set()


def _circuit_info_from_path(path: dict[str, Any], circuits_by_id: dict[int, dict[str, Any]], web_base: str) -> dict[str, Any]:
    circuit_ref = path.get("circuit")
    circuit_id = _fp_id(circuit_ref) if isinstance(circuit_ref, dict) else _fp_id(circuit_ref)
    circ = circuits_by_id.get(circuit_id or 0, {})
    cid = (circ.get("cid") or circ.get("name") or "").strip()
    path_id = path["id"]
    desc = circ.get("description") or circ.get("comments") or ""
    cf = path.get("custom_fields") or {}
    return {
        "cid": cid,
        "name": circ.get("name") or cid,
        "ip": extract_business_ip(desc),
        "description": desc,
        "status": circ.get("status") or "",
        "is_complete": bool(path.get("is_complete")),
        "hops_count": len(path.get("path") or []),
        "path_id": path_id,
        "circuit_id": circuit_id,
        "trace_url": _trace_url_for_path(web_base, path_id),
        "fiber_chain": (cf.get("fiber_chain") or "").strip(),
        "actual_loss_db": path.get("actual_loss_db"),
        "wavelength_nm": path.get("wavelength_nm"),
        "pending": False,
    }


def _merge_fp_index_cache(web_base: str, partial: dict[int, list[dict[str, Any]]]) -> None:
    import time

    web_base = web_base.rstrip("/")
    now = time.time()
    cache = getattr(build_fp_circuit_index, "_cache", None)
    data: dict[int, list[dict[str, Any]]] = {}
    complete = False
    ts = now
    if cache and cache.get("web_base") == web_base:
        data = dict(cache.get("data") or {})
        complete = bool(cache.get("complete"))
        # Partial room scans must not extend a complete index's TTL.
        if complete and cache.get("ts"):
            ts = float(cache.get("ts") or now)
    for fp_id, circs in partial.items():
        data[int(fp_id)] = list(circs)
    build_fp_circuit_index._cache = {
        "ts": ts,
        "web_base": web_base,
        "data": data,
        "complete": complete,
    }


def build_fp_index_for_fp_ids(
    client: NetBoxClient,
    web_base: str,
    target_fp_ids: set[int],
    *,
    include_pending: bool = True,
) -> dict[int, list[dict[str, Any]]]:
    """Scan NetBox paths for occupancy on specific front ports only (fast vs full index rebuild)."""
    web_base = web_base.rstrip("/")
    target_fp_ids = {int(x) for x in target_fp_ids if x}
    if not target_fp_ids:
        return {}

    circuits_by_id = {c["id"]: c for c in paginate(client, "/plugins/fms/fiber-circuits/")}
    netbox_cids = {(c.get("cid") or c.get("name") or "").strip() for c in circuits_by_id.values()}
    indexed_cids: set[str] = set()
    index: dict[int, list[dict[str, Any]]] = {fp_id: [] for fp_id in target_fp_ids}
    store = load_store()

    for path in paginate(client, "/plugins/fms/fiber-circuit-paths/"):
        circuit_ref = path.get("circuit")
        circuit_id = _fp_id(circuit_ref) if isinstance(circuit_ref, dict) else _fp_id(circuit_ref)
        circ = circuits_by_id.get(circuit_id or 0, {})
        cid = (circ.get("cid") or circ.get("name") or "").strip()
        segs = store.get(cid, []) if cid else []
        occupied = occupancy_fp_ids_for_circuit(client, segs, path)
        if not occupied:
            continue
        hit = occupied & target_fp_ids
        if not hit:
            continue
        info = _circuit_info_from_path(path, circuits_by_id, web_base)
        if info.get("cid"):
            indexed_cids.add(info["cid"])
        for fp_id in hit:
            if any(x.get("path_id") == info["path_id"] for x in index[fp_id]):
                continue
            index[fp_id].append(dict(info))

    if include_pending:
        # Only overlay NetBox circuits that are missing a path index hit.
        # Local-only drafts are pruned by reconcile_store_with_netbox on sync.
        for cid, segs in store.items():
            if not segs or not cid:
                continue
            if cid not in netbox_cids:
                continue
            if cid in indexed_cids:
                continue
            route = build_route_short(segs) if segs else ""
            meta = merge_circuit_meta(segs) if segs else {}
            hit_fps = occupancy_fp_ids_for_circuit(client, segs) & target_fp_ids
            if not hit_fps:
                continue
            pending_info = {
                "cid": cid,
                "name": meta.get("name") or cid,
                "ip": meta.get("ip") or "",
                "description": meta.get("service") or "",
                "status": "pending",
                "is_complete": False,
                "hops_count": len(segs),
                "path_id": None,
                "circuit_id": None,
                "trace_url": "",
                "fiber_chain": route,
                "actual_loss_db": meta.get("loss_db") or "",
                "wavelength_nm": meta.get("wavelength") or "",
                "pending": True,
                "cable": "",
                "segment": route,
            }
            for fp_id in hit_fps:
                if any(x.get("cid") == cid for x in index[fp_id]):
                    continue
                index[fp_id].append(dict(pending_info))

    _merge_fp_index_cache(web_base, index)
    return index


def touch_fp_index_cache() -> None:
    """Keep a complete in-memory index from expiring mid full-room warmup."""
    import time

    cache = getattr(build_fp_circuit_index, "_cache", None)
    if cache and cache.get("complete"):
        cache["ts"] = time.time()


def _fp_index_subset(
    data: dict[int, list[dict[str, Any]]],
    target_fp_ids: set[int],
) -> dict[int, list[dict[str, Any]]]:
    """Slice full index for target ports (missing ids => empty list)."""
    return {fp_id: list(data.get(fp_id, [])) for fp_id in target_fp_ids}


def fp_index_for_targets(
    client: NetBoxClient,
    web_base: str,
    target_fp_ids: set[int],
    *,
    force_refresh: bool = False,
) -> dict[int, list[dict[str, Any]]]:
    """Return occupancy subset from memory/disk index; rescan NetBox only when stale."""
    import time

    target_fp_ids = {int(x) for x in target_fp_ids if x}
    if not target_fp_ids:
        return {}
    web_base = web_base.rstrip("/")

    def _merge_missing(data: dict[int, list[dict[str, Any]]]) -> dict[int, list[dict[str, Any]]]:
        missing = {fp_id for fp_id in target_fp_ids if fp_id not in data}
        if not missing:
            return _fp_index_subset(data, target_fp_ids)
        extra = build_fp_index_for_fp_ids(client, web_base, missing)
        merged = dict(data)
        merged.update(extra)
        return {fp_id: list(merged.get(fp_id, [])) for fp_id in target_fp_ids}

    if not force_refresh:
        cache = getattr(build_fp_circuit_index, "_cache", None)
        if cache and cache.get("web_base") == web_base and time.time() - float(cache.get("ts", 0)) < FP_INDEX_MEM_TTL_SEC:
            data = cache.get("data") or {}
            if cache.get("complete"):
                # Complete index: missing keys are idle. Do not per-room rescan
                # (that paginates every FMS path and makes full warmup unusable).
                return _fp_index_subset(data, target_fp_ids)
            if all(fp_id in data for fp_id in target_fp_ids):
                return _fp_index_subset(data, target_fp_ids)
            if any(fp_id in data for fp_id in target_fp_ids):
                return _merge_missing(data)
        disk_index = load_fp_index_from_disk(web_base)
        if disk_index is not None:
            build_fp_circuit_index._cache = {
                "ts": time.time(),
                "web_base": web_base,
                "data": disk_index,
                "complete": True,
            }
            return _fp_index_subset(disk_index, target_fp_ids)
    else:
        # "同步端口状态" used to paginate every FMS path (~8-100s) and starved
        # behind background warmup. Prefer the complete occupancy index.
        cache = getattr(build_fp_circuit_index, "_cache", None)
        if cache and cache.get("web_base") == web_base and cache.get("complete"):
            return _fp_index_subset(cache.get("data") or {}, target_fp_ids)
        disk_index = load_fp_index_from_disk(web_base)
        if disk_index is not None:
            build_fp_circuit_index._cache = {
                "ts": time.time(),
                "web_base": web_base,
                "data": disk_index,
                "complete": True,
            }
            return _fp_index_subset(disk_index, target_fp_ids)
    # No complete index yet: targeted rescan only — do NOT reconcile_store
    # (that paginates all circuits and was making every modal open ~8s).
    return build_fp_index_for_fp_ids(client, web_base, target_fp_ids)


def _fp_index_from_mem_or_disk(web_base: str, *, allow_stale: bool = False) -> dict[int, list[dict[str, Any]]] | None:
    now = time.time()
    cache = getattr(build_fp_circuit_index, "_cache", None)
    if cache and cache.get("web_base") == web_base and cache.get("complete", True):
        if allow_stale or now - cache.get("ts", 0) < FP_INDEX_MEM_TTL_SEC:
            return cache["data"]
    return None


def _fetch_fp_circuit_index(client: NetBoxClient, web_base: str, *, reconcile: bool) -> dict[int, list[dict[str, Any]]]:
    if reconcile:
        reconcile_store_with_netbox(client)

    circuits_by_id: dict[int, dict[str, Any]] = {}
    for circ in paginate(client, "/plugins/fms/fiber-circuits/"):
        circuits_by_id[circ["id"]] = circ

    index: dict[int, list[dict[str, Any]]] = defaultdict(list)
    store = load_store()

    for path in paginate(client, "/plugins/fms/fiber-circuit-paths/"):
        circuit_ref = path.get("circuit")
        circuit_id = _fp_id(circuit_ref) if isinstance(circuit_ref, dict) else _fp_id(circuit_ref)
        circ = circuits_by_id.get(circuit_id or 0, {})
        cid = (circ.get("cid") or circ.get("name") or "").strip()
        segs = store.get(cid, []) if cid else []
        path_id = path["id"]
        desc = circ.get("description") or circ.get("comments") or ""
        cf = path.get("custom_fields") or {}
        info = {
            "cid": cid,
            "name": circ.get("name") or cid,
            "ip": extract_business_ip(desc),
            "description": desc,
            "status": circ.get("status") or "",
            "is_complete": bool(path.get("is_complete")),
            "hops_count": len(path.get("path") or []),
            "path_id": path_id,
            "circuit_id": circuit_id,
            "trace_url": _trace_url_for_path(web_base, path_id),
            "fiber_chain": (cf.get("fiber_chain") or "").strip(),
            "actual_loss_db": path.get("actual_loss_db"),
            "wavelength_nm": path.get("wavelength_nm"),
            "pending": False,
        }
        for fp_id in occupancy_fp_ids_for_circuit(client, segs, path):
            if any(x["path_id"] == path_id for x in index[fp_id]):
                continue
            index[fp_id].append(dict(info))

    netbox_cids = {(c.get("cid") or c.get("name") or "").strip() for c in circuits_by_id.values()}
    path_cids: set[str] = set()
    for fp_list in index.values():
        for item in fp_list:
            if item.get("cid"):
                path_cids.add(item["cid"])
    for cid, segs in store.items():
        if not segs or not cid:
            continue
        if cid not in netbox_cids:
            continue
        if cid in path_cids:
            continue
        route = build_route_short(segs) if segs else ""
        meta = merge_circuit_meta(segs) if segs else {}
        pending_info = {
            "cid": cid,
            "name": meta.get("name") or cid,
            "ip": meta.get("ip") or "",
            "description": meta.get("service") or "",
            "status": "pending",
            "is_complete": False,
            "hops_count": len(segs),
            "path_id": None,
            "circuit_id": None,
            "trace_url": "",
            "fiber_chain": route,
            "actual_loss_db": meta.get("loss_db") or "",
            "wavelength_nm": meta.get("wavelength") or "",
            "pending": True,
            "cable": "",
            "segment": route,
        }
        for fp_id in occupancy_fp_ids_for_circuit(client, segs):
            if any(x["cid"] == cid for x in index[fp_id]):
                continue
            index[fp_id].append(dict(pending_info))
    return dict(index)


def build_fp_circuit_index(
    client: NetBoxClient,
    web_base: str,
    *,
    force_refresh: bool = False,
) -> dict[int, list[dict[str, Any]]]:
    web_base = web_base.rstrip("/")
    if not force_refresh:
        hit = _fp_index_from_mem_or_disk(web_base)
        if hit is not None:
            return hit

    lock = _fp_index_lock()
    if not force_refresh:
        with lock:
            hit = _fp_index_from_mem_or_disk(web_base)
            if hit is not None:
                return hit
            disk_index = load_fp_index_from_disk(web_base)
            if disk_index is not None:
                build_fp_circuit_index._cache = {
                    "ts": time.time(),
                    "web_base": web_base,
                    "data": disk_index,
                    "complete": True,
                }
                return disk_index
        stale = _fp_index_from_mem_or_disk(web_base, allow_stale=True)
        if stale is not None:
            return stale

    with _fp_index_rebuild_lock():
        if not force_refresh:
            with lock:
                hit = _fp_index_from_mem_or_disk(web_base)
                if hit is not None:
                    return hit
                disk_index = load_fp_index_from_disk(web_base)
                if disk_index is not None:
                    build_fp_circuit_index._cache = {
                        "ts": time.time(),
                        "web_base": web_base,
                        "data": disk_index,
                        "complete": True,
                    }
                    return disk_index
        index = _fetch_fp_circuit_index(client, web_base, reconcile=force_refresh)
        with lock:
            build_fp_circuit_index._cache = {
                "ts": time.time(),
                "web_base": web_base,
                "data": index,
                "complete": True,
            }
            save_fp_index_to_disk(web_base, index)
        return index


def _trace_hops_summary(trace: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for hop in trace.get("hops") or []:
        ht = hop.get("type")
        if ht == "cable":
            lines.append(f"缆段 {hop.get('label', '')}")
        elif ht == "port_link":
            pa = hop.get("port_a") or {}
            pb = hop.get("port_b") or {}
            da = (pa.get("device") or {}).get("name", "")
            db = (pb.get("device") or {}).get("name", "")
            lines.append(f"{hop.get('link_kind', '跳纤')}: {da}:{pa.get('name','')} ↔ {db}:{pb.get('name','')}")
        elif ht == "device":
            name = hop.get("name", "")
            if hop.get("ports", {}).get("front_port"):
                fp = hop["ports"]["front_port"]
                lines.append(f"{name} · {fp.get('name','')}（端点）")
            else:
                ing = (hop.get("ingress") or {}).get("front_port") or {}
                egr = (hop.get("egress") or {}).get("front_port") or {}
                if ing.get("name") and egr.get("name") and ing["name"] != egr["name"]:
                    lines.append(f"{name}: {ing['name']} → {egr['name']}（熔接/换缆）")
                elif ing.get("name"):
                    lines.append(f"{name}: {ing['name']}")
    return lines


def _trace_timeline(trace: dict[str, Any], current_odf: str = "", client: NetBoxClient | None = None) -> list[dict[str, Any]]:
    """Structured hops for frontend timeline chart."""
    steps: list[dict[str, Any]] = []
    for hop in trace.get("hops") or []:
        ht = hop.get("type")
        if ht == "cable":
            steps.append(
                {
                    "kind": "cable",
                    "title": hop.get("label") or "缆段",
                    "subtitle": "户外 / 楼间光缆",
                    "detail": "",
                    "is_current": False,
                }
            )
            continue
        if ht == "port_link":
            pa = hop.get("port_a") or {}
            pb = hop.get("port_b") or {}
            da = (pa.get("device") or {}).get("name", "")
            db = (pb.get("device") or {}).get("name", "")
            pa_name = pa.get("name", "")
            pb_name = pb.get("name", "")
            if client and da == db == current_odf:
                cab_a = cable_for_port_on_odf(client, da, pa_name)
                cab_b = cable_for_port_on_odf(client, db, pb_name)
                la = route_display_name(client, da, cab_a)
                lb = route_display_name(client, db, cab_b)
                fa = fiber_from_port_name(pa_name) or pa_name
                fb = fiber_from_port_name(pb_name) or pb_name
                subtitle = f"{la} {fa}"
                detail = f"↔ {lb} {fb}"
            elif client:
                la = route_display_name(client, da, cable_for_port_on_odf(client, da, pa_name))
                lb = route_display_name(client, db, cable_for_port_on_odf(client, db, pb_name))
                subtitle = f"{la}:{pa_name}"
                detail = f"↔ {lb}:{pb_name}"
            else:
                subtitle = f"{da}:{pa_name}"
                detail = f"↔ {db}:{pb_name}"
            steps.append(
                {
                    "kind": "jump",
                    "title": hop.get("link_kind") or "跳纤",
                    "subtitle": subtitle,
                    "detail": detail,
                    "is_current": current_odf in (da, db),
                }
            )
            continue
        if ht != "device":
            continue
        name = hop.get("name") or ""
        is_current = name == current_odf
        disp = route_display_name(client, name) if client else name
        if hop.get("ports", {}).get("front_port"):
            fp = hop["ports"]["front_port"]
            rp = hop.get("ports", {}).get("rear_port") or {}
            rp_name = rp.get("name", "") if isinstance(rp, dict) else str(rp)
            steps.append(
                {
                    "kind": "odf",
                    "title": disp,
                    "subtitle": fp.get("name") or "",
                    "detail": f"业务端点 · {rp_name}" if rp_name else "业务端点",
                    "is_current": is_current,
                }
            )
            continue
        ing = hop.get("ingress") or {}
        egr = hop.get("egress") or {}
        ing_fp = (ing.get("front_port") or {}).get("name", "")
        egr_fp = (egr.get("front_port") or {}).get("name", "")
        ing_rp = (ing.get("rear_port") or {}).get("name", "")
        egr_rp = (egr.get("rear_port") or {}).get("name", "")
        splice = hop.get("splice") or {}
        kind = "splice" if splice else "odf"
        title_extra = (splice.get("plan_name") or "熔接/换缆") if splice else "ODF 框"
        detail_bits = []
        if ing_fp and ing_rp:
            detail_bits.append(f"入纤 {ing_fp} ← {ing_rp}")
        if egr_fp and egr_rp and egr_fp != ing_fp:
            detail_bits.append(f"出纤 {egr_fp} → {egr_rp}")
        steps.append(
            {
                "kind": kind,
                "title": disp,
                "subtitle": f"{ing_fp} → {egr_fp}" if ing_fp and egr_fp and ing_fp != egr_fp else (ing_fp or egr_fp),
                "detail": " · ".join(detail_bits) or title_extra,
                "is_current": is_current,
            }
        )
    return steps


def route_display_name(client: NetBoxClient, odf_name: str, cable_label: str = "") -> str:
    """UI route label e.g. 轧钢至白灰窑 (not NetBox device name)."""
    from field_browser import _display_name_for_route, _rear_ports_by_device_id, parse_odf_name

    odf_name = (odf_name or "").strip()
    cable_label = (cable_label or "").strip()
    cache_key = f"{odf_name}|{cable_label}"
    with _DISPLAY_CACHE_LOCK:
        hit = _ROUTE_DISP_MEM.get(cache_key)
        if hit is not None:
            return hit
    try:
        dev = _cached_find_device(client, odf_name)
    except Exception:
        with _DISPLAY_CACHE_LOCK:
            _ROUTE_DISP_MEM[cache_key] = odf_name
        return odf_name
    loc = dev.get("location") or {}
    room = (loc.get("name", "") if isinstance(loc, dict) else "") or parse_odf_name(odf_name).get("room", "")
    rb = _rear_ports_by_device_id(client)
    name = odf_name
    if cable_label:
        for cab in cable_label_variants(cable_label):
            disp, _ = _display_name_for_route(client, odf_name, room, dev["id"], cab, rear_by_dev=rb)
            if disp and disp != odf_name:
                name = disp
                break
        else:
            name, _ = _display_name_for_route(client, odf_name, room, dev["id"], cable_label, rear_by_dev=rb)
    else:
        name, _ = _display_name_for_route(client, odf_name, room, dev["id"], cable_label, rear_by_dev=rb)
    name = name or odf_name
    with _DISPLAY_CACHE_LOCK:
        _ROUTE_DISP_MEM[cache_key] = name
    return name


def peer_route_display_name(client: NetBoxClient, peer_odf: str, cable_label: str) -> str:
    from field_browser import _peer_display_name, _rear_ports_by_device_id

    return _peer_display_name(client, peer_odf, cable_label, rear_by_dev=_rear_ports_by_device_id(client))


def cable_for_port_on_odf(client: NetBoxClient, odf_name: str, port_name: str) -> str:
    port_name = (port_name or "").strip()
    odf_name = (odf_name or "").strip()
    if not port_name or not odf_name:
        return ""
    cache_key = f"{odf_name}|{port_name}"
    with _DISPLAY_CACHE_LOCK:
        if cache_key in _CABLE_FOR_PORT_MEM:
            return _CABLE_FOR_PORT_MEM[cache_key]
    from field_browser import _rear_ports_by_device_id

    local_room = local_room_from_odf_name(odf_name)
    try:
        dev = _cached_find_device(client, odf_name)
    except Exception:
        with _DISPLAY_CACHE_LOCK:
            _CABLE_FOR_PORT_MEM[cache_key] = ""
        return ""
    labels = _rear_ports_by_device_id(client).get(int(dev["id"]), [])
    found = ""
    for lbl in labels:
        if port_name.startswith(cable_port_prefix(lbl, local_room)):
            found = lbl
            break
    with _DISPLAY_CACHE_LOCK:
        _CABLE_FOR_PORT_MEM[cache_key] = found
    return found


def _trace_hops_at_odf(trace: dict[str, Any], odf_name: str, client: NetBoxClient | None = None) -> list[str]:
    """Only hops involving the current ODF (this room's front panel scope)."""
    lines: list[str] = []
    for hop in trace.get("hops") or []:
        ht = hop.get("type")
        if ht == "cable":
            continue
        if ht == "port_link":
            pa = hop.get("port_a") or {}
            pb = hop.get("port_b") or {}
            da = (pa.get("device") or {}).get("name", "")
            db = (pb.get("device") or {}).get("name", "")
            if odf_name not in (da, db):
                continue
            pa_name = pa.get("name", "")
            pb_name = pb.get("name", "")
            if client and da == db == odf_name:
                cab_a = cable_for_port_on_odf(client, da, pa_name)
                cab_b = cable_for_port_on_odf(client, db, pb_name)
                la = route_display_name(client, da, cab_a)
                lb = route_display_name(client, db, cab_b)
                fa = fiber_from_port_name(pa_name) or pa_name
                fb = fiber_from_port_name(pb_name) or pb_name
                lines.append(f"{hop.get('link_kind', '跳纤')}: {la} {fa} ↔ {lb} {fb}")
            elif client:
                la = route_display_name(client, da, cable_for_port_on_odf(client, da, pa_name))
                lb = route_display_name(client, db, cable_for_port_on_odf(client, db, pb_name))
                lines.append(f"{hop.get('link_kind', '跳纤')}: {la}:{pa_name} ↔ {lb}:{pb_name}")
            else:
                lines.append(f"{hop.get('link_kind', '跳纤')}: {da}:{pa_name} ↔ {db}:{pb_name}")
        elif ht == "device":
            name = hop.get("name", "")
            if name != odf_name:
                continue
            disp = route_display_name(client, name) if client else name
            if hop.get("ports", {}).get("front_port"):
                fp = hop["ports"]["front_port"]
                lines.append(f"{disp} · {fp.get('name','')}（端点）")
            else:
                ing = (hop.get("ingress") or {}).get("front_port") or {}
                egr = (hop.get("egress") or {}).get("front_port") or {}
                ing_name = ing.get("name", "")
                egr_name = egr.get("name", "")
                if ing_name and egr_name and ing_name != egr_name:
                    ing_f = fiber_from_port_name(ing_name) or ing_name
                    egr_f = fiber_from_port_name(egr_name) or egr_name
                    ing_cab = cable_for_port_on_odf(client, name, ing_name) if client else ""
                    egr_cab = cable_for_port_on_odf(client, name, egr_name) if client else ""
                    ing_l = route_display_name(client, name, ing_cab) if client else disp
                    egr_l = route_display_name(client, name, egr_cab) if client else disp
                    if client and ing_l == egr_l:
                        lines.append(f"{ing_l}: {ing_f} → {egr_f}（本框换缆）")
                    elif client:
                        lines.append(f"{ing_l} {ing_f} → {egr_l} {egr_f}（本框换缆）")
                    else:
                        lines.append(f"{name}: {ing_name} → {egr_name}（本框换缆）")
                elif ing_name:
                    lines.append(f"{disp}: {ing_name}")
    return lines


def _segment_cable_matches(seg_cable: str, panel_cable: str) -> bool:
    if not seg_cable or not panel_cable:
        return True
    if seg_cable == panel_cable:
        return True

    def endpoints(lbl: str) -> frozenset[str]:
        p = parse_route_label(lbl)
        if p["room_a"] and p["room_b"] and "至" in lbl:
            return frozenset({normalize_room_name(p["room_a"]), normalize_room_name(p["room_b"])})
        parts = lbl.upper().replace("CBL-", "").split("-")
        return frozenset(parts[:2]) if len(parts) >= 2 else frozenset(parts)

    return endpoints(seg_cable) == endpoints(panel_cable)


def local_segment_at_odf(
    odf_name: str,
    cable: str,
    fiber: str,
    peer_odf: str = "",
    *,
    client: NetBoxClient | None = None,
) -> dict[str, str]:
    """Business segment at this ODF only — not the full multi-room route."""
    store = load_store()
    local_name = resolve_port_name(fiber, cable, device_name=odf_name) if cable else fiber

    def _fmt_peer(dev_name: str, port: str, seg_cable: str) -> str:
        if not dev_name:
            return port
        if client:
            label = peer_route_display_name(client, dev_name, seg_cable or cable)
            pf = fiber_from_port_name(port) or port
            return f"{label} {pf}"
        return f"{dev_name} {port}"

    def _panel_cable(seg_cable: str) -> str:
        if client and seg_cable:
            on_odf = cable_label_on_odf(client, odf_name, peer_odf, seg_cable)
            return on_odf or seg_cable
        return seg_cable or cable

    for _cid, segs in store.items():
        for seg in segs:
            if cable and seg.get("cable") and not _segment_cable_matches(seg.get("cable", ""), cable):
                continue
            port_a = seg.get("port_a") or ""
            port_b = seg.get("port_b") or ""
            seg_cable = seg.get("cable") or cable
            if seg.get("dev_a") == odf_name and (port_a == fiber or port_a == local_name):
                peer_dev = seg.get("dev_b") or peer_odf
                peer_port = seg.get("port_b") or fiber
                return {
                    "cable": _panel_cable(seg_cable),
                    "local_odf": odf_name,
                    "local_fiber": fiber,
                    "peer_odf": peer_dev,
                    "peer_fiber": peer_port,
                    "summary": f"{fiber} → {_fmt_peer(peer_dev, peer_port, seg_cable)}",
                }
            if seg.get("dev_b") == odf_name and (port_b == fiber or port_b == local_name):
                peer_dev = seg.get("dev_a") or peer_odf
                peer_port = seg.get("port_a") or fiber
                return {
                    "cable": _panel_cable(seg_cable),
                    "local_odf": odf_name,
                    "local_fiber": fiber,
                    "peer_odf": peer_dev,
                    "peer_fiber": peer_port,
                    "summary": f"{fiber} → {_fmt_peer(peer_dev, peer_port, seg_cable)}",
                }
    peer = peer_odf or "对端 ODF"
    peer_label = peer_route_display_name(client, peer, cable) if client and peer_odf else peer
    return {
        "cable": cable,
        "local_odf": odf_name,
        "local_fiber": fiber,
        "peer_odf": peer,
        "peer_fiber": fiber,
        "summary": f"{fiber} ↔ {peer_label} {fiber}",
    }


def _registered_endpoints_in_trace(
    trace: dict[str, Any],
    segs: list[dict[str, str]],
    client: NetBoxClient,
) -> bool:
    """True when FMS trace reaches the last registered segment's B-side port."""
    if not segs:
        return True
    ordered = _orient_segments_for_explicit_path(segs)
    last = ordered[-1] if ordered else segs[-1]
    end_dev = (last.get("dev_b") or "").strip()
    end_fiber = (last.get("port_b") or "").strip()
    end_cab = (last.get("cab_b") or last.get("cable") or "").strip()
    if not end_dev or not end_fiber:
        return True
    end_name = resolve_port_name(end_fiber, end_cab, device_name=end_dev)
    for hop in trace.get("hops") or []:
        ht = hop.get("type")
        if ht == "port_link":
            for side in ("port_a", "port_b"):
                p = hop.get(side) or {}
                if (p.get("device") or {}).get("name") == end_dev and p.get("name") == end_name:
                    return True
        if ht != "device":
            continue
        for key in ("ports", "ingress", "egress"):
            block = hop.get(key) or {}
            fp = block.get("front_port") if isinstance(block, dict) else None
            if not isinstance(fp, dict):
                continue
            if hop.get("name") == end_dev and fp.get("name") == end_name:
                return True
    return False


def _trace_device_endpoint(hop: dict[str, Any], client: NetBoxClient | None) -> tuple[str, str, str] | None:
    """Origin/destination ODF hop → (device_name, fiber_label, rear_cable)."""
    if hop.get("type") != "device":
        return None
    ports = hop.get("ports") or {}
    fp = ports.get("front_port")
    if not isinstance(fp, dict) or not fp.get("name"):
        return None
    odf = (hop.get("name") or "").strip()
    fp_name = (fp.get("name") or "").strip()
    rp = ports.get("rear_port") or {}
    rp_name = (rp.get("name") or "").strip() if isinstance(rp, dict) else ""
    cab = rp_name if rp_name and is_trunk_cable_label(rp_name) else ""
    if not cab and client and odf:
        cab = cable_for_port_on_odf(client, odf, fp_name) or ""
    fiber = fiber_from_port_name(fp_name) or fp_name
    return odf, fiber, cab


def _find_trace_endpoint(hops: list[dict[str, Any]], start: int, step: int, client: NetBoxClient | None):
    i = start
    while 0 <= i < len(hops):
        ep = _trace_device_endpoint(hops[i], client)
        if ep:
            return ep
        i += step
    return None


def _last_trace_front_endpoint(
    hops: list[dict[str, Any]],
    client: NetBoxClient | None,
) -> tuple[str, str, str] | None:
    for hop in reversed(hops or []):
        ep = _trace_device_endpoint(hop, client)
        if ep:
            return ep
    return None


def _infer_tail_trunk_segment(
    segs: list[dict[str, str]],
    trace: dict[str, Any],
    cid: str,
    client: NetBoxClient,
) -> dict[str, str] | None:
    """When FMS ends on a trunk port but store has no final cable row, infer it."""
    from naming_rules import cable_label_variants

    hops = trace.get("hops") or []
    dest = _last_trace_front_endpoint(hops, client)
    if not dest:
        return None
    dest_dev, dest_fiber, dest_cab = dest
    if not dest_dev or not dest_fiber or not dest_cab:
        return None
    peer = resolve_peer_from_cable(client, dest_dev, dest_cab)
    if not peer:
        return None
    peer_cab = cable_label_on_odf(client, peer, dest_dev, dest_cab)
    cab_label = dest_cab or cable_label_on_odf(client, dest_dev, peer, dest_cab)
    cab_vars = set(cable_label_variants(cab_label))

    ordered = _ordered_store_segments(segs)
    for seg in ordered:
        if _is_jump_segment(seg):
            continue
        seg_cab = (seg.get("cable") or "").strip()
        if seg_cab and not (cab_vars & set(cable_label_variants(seg_cab))):
            continue
        ends = {(seg.get("dev_a"), seg.get("port_a")), (seg.get("dev_b"), seg.get("port_b"))}
        if (dest_dev, dest_fiber) in ends and peer in (seg.get("dev_a"), seg.get("dev_b")):
            return None

    meta = merge_circuit_meta(ordered) if ordered else {"name": cid}
    seg = segment_from_field(
        dest_dev,
        peer,
        "a",
        dest_fiber,
        dest_fiber,
        cid=cid,
        cable=cab_label,
        name=meta.get("name") or cid,
        service=meta.get("service") or cid,
    )
    seg["cab_a"] = cab_label
    seg["cab_b"] = peer_cab
    return seg


def segments_from_fms_trace(
    trace: dict[str, Any],
    cid: str,
    client: NetBoxClient,
    *,
    display_name: str = "",
) -> list[dict[str, str]]:
    """Rebuild portal segment rows from FMS path trace (when circuit_store has no entry)."""
    hops = trace.get("hops") or []
    if not hops or not cid:
        return []
    name = (display_name or cid).strip()
    segments: list[dict[str, str]] = []
    for i, hop in enumerate(hops):
        ht = hop.get("type")
        if ht == "cable":
            prev = _find_trace_endpoint(hops, i - 1, -1, client)
            nxt = _find_trace_endpoint(hops, i + 1, 1, client)
            if not prev or not nxt:
                continue
            da, fa, cab_a = prev
            db, fb, cab_b = nxt
            cab_label = (hop.get("label") or "").strip() or cab_a or cab_b
            seg = segment_from_field(da, db, "a", fa, fb, cid=cid, cable=cab_label, name=name, service=name)
            seg["cab_a"] = cab_a or cab_label
            seg["cab_b"] = cab_b or cab_label
            segments.append(seg)
            continue
        if ht == "port_link":
            pa = hop.get("port_a") or {}
            pb = hop.get("port_b") or {}
            da = ((pa.get("device") or {}).get("name") or "").strip()
            db = ((pb.get("device") or {}).get("name") or "").strip()
            pa_name = (pa.get("name") or "").strip()
            pb_name = (pb.get("name") or "").strip()
            if not da or not db or not pa_name or not pb_name:
                continue
            fa = fiber_from_port_name(pa_name) or pa_name
            fb = fiber_from_port_name(pb_name) or pb_name
            cab_a = cable_for_port_on_odf(client, da, pa_name) if client else ""
            cab_b = cable_for_port_on_odf(client, db, pb_name) if client else ""
            cab = cab_a or cab_b
            seg = segment_from_field(da, db, "a", fa, fb, cid=cid, cable=cab, name=name, service=name)
            seg["cab_a"] = cab_a
            seg["cab_b"] = cab_b
            seg["label_pos"] = f"{da} {fa} -> {db} {fb} (跳纤段)"
            segments.append(seg)
    return dedupe_segment_rows(segments)


def resolve_circuit_segments(
    cid: str,
    trace: dict[str, Any] | None,
    store: dict[str, list[dict[str, str]]],
    client: NetBoxClient,
    *,
    display_name: str = "",
    persist_inferred: bool = False,
) -> tuple[list[dict[str, str]] | None, bool]:
    """Store rows first; merge FMS-inferred tail trunk. Returns (segments, inferred)."""
    inferred = False
    segs: list[dict[str, str]] | None = None
    if cid and store.get(cid):
        segs = list(store[cid])
    elif cid and trace:
        segs = segments_from_fms_trace(trace, cid, client, display_name=display_name)
        inferred = bool(segs)
    if not segs or not trace or not cid:
        return segs, inferred
    tail = _infer_tail_trunk_segment(segs, trace, cid, client)
    if not tail:
        return segs, inferred
    merged = dedupe_segment_rows(_ordered_store_segments(segs) + [tail])
    if persist_inferred:
        existing = store.get(cid) or []
        # Never shrink a richer local registration with a shorter inferred merge.
        if len(merged) >= len(existing):
            store[cid] = merged
            save_store(store)
    return merged, True


def _resolve_ordered_segments_for_circuit(
    client: NetBoxClient,
    cid: str,
    *,
    path_id: int | None = None,
    web_base: str = "",
) -> list[dict[str, str]]:
    store = load_store()
    trace: dict[str, Any] = {}
    if path_id and web_base:
        try:
            trace = get_fms_path_trace(client, int(path_id), web_base, force_refresh=False)
        except Exception:
            trace = {}
    segs, _ = resolve_circuit_segments(cid, trace or None, store, client, persist_inferred=False)
    return _ordered_store_segments(segs or store.get(cid, []))


def ensure_circuit_segments_in_store(
    config: dict[str, Any],
    client: NetBoxClient,
    cid: str,
    *,
    path_id: int | None = None,
    display_name: str = "",
) -> list[dict[str, str]]:
    store = load_store()
    if store.get(cid):
        return store[cid]
    pid = path_id
    if not pid:
        hits = client.request("GET", "/plugins/fms/fiber-circuits/", params={"cid": cid, "limit": 1})
        if hits.get("results"):
            paths = client.request(
                "GET",
                "/plugins/fms/fiber-circuit-paths/",
                params={"circuit_id": hits["results"][0]["id"], "limit": 1},
            )
            if paths.get("results"):
                pid = int(paths["results"][0]["id"])
    if not pid:
        raise ValueError(f"光路 {cid} 无登记段且找不到 FMS 路径，请先在业务页登记")
    web_base = config.get("web_base_url", config["base_url"].replace("/api", ""))
    trace = get_fms_path_trace(client, int(pid), web_base, force_refresh=True)
    segs = segments_from_fms_trace(trace, cid, client, display_name=display_name)
    if not segs:
        raise ValueError(f"光路 {cid} 无法从追踪推断缆段，请先在业务页登记")
    store[cid] = segs
    save_store(store)
    return segs


def _is_jump_segment(seg: dict[str, str]) -> bool:
    from naming_rules import cable_belongs_on_route_odf

    da = (seg.get("dev_a") or "").strip()
    cab = (seg.get("cab_a") or seg.get("cable") or "").strip()
    if "(跳纤段)" in (seg.get("label_pos") or ""):
        return True
    return bool(da and cab and not cable_belongs_on_route_odf(da, cab))


def _swap_meta(seg: dict[str, str], index: int, cid: str, client: NetBoxClient) -> dict[str, Any]:
    cab_a = (seg.get("cab_a") or seg.get("cable") or "").strip()
    cab_b = (seg.get("cab_b") or seg.get("cable") or "").strip()
    da = (seg.get("dev_a") or "").strip()
    db = (seg.get("dev_b") or "").strip()
    return {
        "cid": cid,
        "segment_index": index,
        "segment_type": "jump" if _is_jump_segment(seg) else "trunk",
        "dev_a": da,
        "dev_b": db,
        "port_a": (seg.get("port_a") or "").strip(),
        "port_b": (seg.get("port_b") or "").strip(),
        "cable": (seg.get("cable") or "").strip(),
        "cab_a": cab_a,
        "cab_b": cab_b,
        "label_a": route_display_name(client, da, cab_a),
        "label_b": route_display_name(client, db, cab_b),
    }


def _ordered_store_segments(segs: list[dict[str, str]]) -> list[dict[str, str]]:
    from import_from_excel import auto_order_segments

    return auto_order_segments(list(segs)) if segs else []


def _enrich_timeline_with_swap(
    steps: list[dict[str, Any]],
    segs: list[dict[str, str]],
    cid: str,
    client: NetBoxClient,
) -> list[dict[str, Any]]:
    """Attach swap metadata to cable/jump hops (FMS trace or store timeline)."""
    from naming_rules import cable_label_variants

    ordered = _ordered_store_segments(segs)
    if not ordered or not cid:
        return steps
    seg_i = 0
    for step in steps:
        if seg_i >= len(ordered):
            break
        seg = ordered[seg_i]
        is_jump = _is_jump_segment(seg)
        kind = step.get("kind")
        if is_jump and kind == "jump":
            step["swap"] = _swap_meta(seg, seg_i, cid, client)
            seg_i += 1
            continue
        if not is_jump and kind == "cable":
            cab = (seg.get("cable") or seg.get("cab_a") or seg.get("cab_b") or "").strip()
            title = (step.get("title") or "").strip()
            variants = set(cable_label_variants(cab)) if cab else set()
            if not cab or title in variants or title == cab:
                step["swap"] = _swap_meta(seg, seg_i, cid, client)
                seg_i += 1
    return steps


def _timeline_from_registered_segments(
    segs: list[dict[str, str]],
    client: NetBoxClient,
    current_odf: str = "",
    *,
    cid: str = "",
) -> list[dict[str, Any]]:
    """Portal registration rows → timeline when FMS retrace stops early (cross-fiber + jump)."""
    from naming_rules import cable_belongs_on_route_odf

    ordered = _ordered_store_segments(segs)
    steps: list[dict[str, Any]] = []
    for i, seg in enumerate(ordered):
        cab = (seg.get("cable") or "").strip()
        cab_a = (seg.get("cab_a") or cab).strip()
        cab_b = (seg.get("cab_b") or cab).strip()
        da = (seg.get("dev_a") or "").strip()
        db = (seg.get("dev_b") or "").strip()
        pa = (seg.get("port_a") or "").strip()
        pb = (seg.get("port_b") or "").strip()
        is_jump = "(跳纤段)" in (seg.get("label_pos") or "") or (
            da and cab and not cable_belongs_on_route_odf(da, cab)
        )

        if i == 0:
            disp_a = route_display_name(client, da, cab_a if not is_jump else cab_a)
            steps.append(
                {
                    "kind": "odf",
                    "title": disp_a,
                    "subtitle": pa,
                    "detail": f"业务端点 · {cab_a or cab}",
                    "is_current": da == current_odf,
                }
            )

        if is_jump:
            la = route_display_name(client, da, cab_a)
            lb = route_display_name(client, db, cab_b)
            jump_step: dict[str, Any] = {
                "kind": "jump",
                "title": "跳纤",
                "subtitle": f"{la} {pa}",
                "detail": f"↔ {lb} {pb}",
                "is_current": current_odf in (da, db),
            }
            if cid:
                jump_step["swap"] = _swap_meta(seg, i, cid, client)
            steps.append(jump_step)
            steps.append(
                {
                    "kind": "odf",
                    "title": lb,
                    "subtitle": pb,
                    "detail": f"业务端点 · {cab_b or cab}",
                    "is_current": db == current_odf,
                }
            )
            continue

        cable_title = cab or cab_a or cab_b
        cable_step: dict[str, Any] = {
            "kind": "cable",
            "title": cable_title,
            "subtitle": "户外 / 楼间光缆",
            "detail": "",
            "is_current": False,
        }
        if cid:
            cable_step["swap"] = _swap_meta(seg, i, cid, client)
        steps.append(cable_step)
        disp_b = route_display_name(client, db, cab)
        if pa != pb:
            detail = f"入纤 {pb} ← {cab}（异纤 {pa}→{pb}）"
        else:
            detail = f"入纤 {pb} ← {cab}"
        steps.append(
            {
                "kind": "odf",
                "title": disp_b,
                "subtitle": pb,
                "detail": detail,
                "is_current": db == current_odf,
            }
        )
    return steps


def _apply_trace_to_circuit_item(
    item: dict[str, Any],
    trace: dict[str, Any],
    odf_name: str,
    client: NetBoxClient,
    *,
    store_segments: list[dict[str, str]] | None = None,
    segments_inferred: bool = False,
) -> None:
    item["local_hops"] = _trace_hops_at_odf(trace, odf_name, client=client)
    cid = (item.get("cid") or "").strip()
    use_store = bool(store_segments)
    if use_store:
        item["trace_timeline"] = _timeline_from_registered_segments(
            store_segments, client, odf_name, cid=cid
        )
        item["trace_source"] = "store"
    else:
        item["trace_timeline"] = _trace_timeline(trace, odf_name, client=client)
        if store_segments and cid:
            item["trace_timeline"] = _enrich_timeline_with_swap(
                item["trace_timeline"], store_segments, cid, client
            )
        item["trace_source"] = "fms"
    path_id = item.get("path_id")
    if path_id and item.get("trace_timeline"):
        for step in item["trace_timeline"]:
            swap = step.get("swap")
            if isinstance(swap, dict):
                swap["path_id"] = int(path_id)
                if segments_inferred:
                    swap["inferred"] = True
    item["total_loss_db"] = trace.get("total_actual_loss_db") or item.get("actual_loss_db")
    item["wavelength_nm"] = trace.get("wavelength_nm")
    item["is_complete"] = trace.get("is_complete", item.get("is_complete"))
    item["full_route"] = build_fiber_chain_from_trace(trace)[0]
    item["hop_count"] = len(trace.get("hops") or [])


def get_port_occupancy(
    client: NetBoxClient,
    odf_name: str,
    fp_id: int | None,
    cable: str,
    fiber: str,
    web_base: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fresh in_use/cid for one front port via targeted NetBox path scan."""
    resolved_fp_id = fp_id
    if odf_name and fiber and cable:
        try:
            dev = find_device(client, odf_name)
            fp = find_front_port(
                client,
                dev["id"],
                resolve_port_name(fiber, cable, device_name=odf_name),
                fiber,
            )
            resolved_fp_id = int(fp["id"])
        except Exception:
            pass
    fp_id = resolved_fp_id
    if fp_id:
        index = fp_index_for_targets(client, web_base, {int(fp_id)}, force_refresh=force_refresh)
        circuits = list(index.get(int(fp_id), []))
    else:
        circuits = []
    primary = circuits[0] if circuits else {}
    return {
        "odf": odf_name,
        "cable": cable,
        "fiber": fiber,
        "fp_id": fp_id,
        "in_use": len(circuits) > 0,
        "cid": (primary.get("cid") or "").strip(),
        "pending": bool(primary.get("pending")),
    }


def get_port_business_detail(
    client: NetBoxClient,
    odf_name: str,
    fp_id: int | None,
    cable: str,
    fiber: str,
    web_base: str,
) -> dict[str, Any]:
    cache_key = f"{odf_name}|{fp_id or ''}|{cable}|{fiber}"
    now = time.time()
    with _DISPLAY_CACHE_LOCK:
        hit = _PORT_DETAIL_MEM.get(cache_key)
        if hit and now - float(hit.get("ts", 0)) < _PORT_DETAIL_TTL_SEC:
            return dict(hit["data"])

    resolved_fp_id = fp_id
    if not resolved_fp_id and odf_name and fiber and cable:
        try:
            dev = _cached_find_device(client, odf_name)
            fp = find_front_port(
                client,
                dev["id"],
                resolve_port_name(fiber, cable, device_name=odf_name),
                fiber,
            )
            resolved_fp_id = int(fp["id"])
        except Exception:
            pass
    fp_id = resolved_fp_id
    if fp_id:
        index = fp_index_for_targets(client, web_base, {int(fp_id)})
        circuits = list(index.get(int(fp_id), []))
    else:
        circuits = []
    store = load_store()

    peer_odf = resolve_peer_from_cable(client, odf_name, cable)
    local_seg = local_segment_at_odf(odf_name, cable, fiber, peer_odf, client=client)
    local = describe_front_port_local(client, fp_id, odf_name=odf_name, cable_label=cable) if fp_id else {"local_type": "empty", "summary": "", "targets": []}

    enriched: list[dict[str, Any]] = []
    for circ in circuits:
        item = dict(circ)
        item["local_segment"] = local_seg
        item.pop("fiber_chain", None)
        if circ.get("path_id") and not circ.get("pending"):
            try:
                trace = get_fms_path_trace(client, int(circ["path_id"]), web_base)
                cid = (circ.get("cid") or "").strip()
                # Do not persist inferred tails here — that left store ahead of NetBox
                # (portal showed 3 段 while FMS path still ended at 跳纤落地口).
                segs, inferred = resolve_circuit_segments(
                    cid,
                    trace,
                    store,
                    client,
                    display_name=(circ.get("name") or cid),
                    persist_inferred=False,
                )
                _apply_trace_to_circuit_item(
                    item,
                    trace,
                    odf_name,
                    client,
                    store_segments=segs,
                    segments_inferred=inferred,
                )
            except Exception as exc:
                item["trace_error"] = str(exc)
        elif circ.get("pending"):
            item["local_hops"] = [local_seg.get("summary", "")]
            cid = (circ.get("cid") or "").strip()
            pending_segs = store.get(cid) if cid else None
            if pending_segs:
                item["trace_timeline"] = _timeline_from_registered_segments(
                    pending_segs, client, odf_name, cid=cid
                )
                item["trace_source"] = "store"
        enriched.append(item)

    result = {
        "odf": odf_name,
        "cable": cable,
        "fiber": fiber,
        "fp_id": fp_id,
        "in_use": len(enriched) > 0,
        "circuits": enriched,
        "local": local,
        "local_segment": local_seg,
    }
    if not any(c.get("trace_error") for c in enriched):
        with _DISPLAY_CACHE_LOCK:
            _PORT_DETAIL_MEM[cache_key] = {"ts": now, "data": result}
    return result


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or Path(__file__).resolve().parent.parent / "netbox_config.json"
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def client_from_config(config: dict[str, Any]) -> NetBoxClient:
    return NetBoxClient(config["base_url"], config["token"], config.get("verify_ssl", True))


def load_store() -> dict[str, list[dict[str, str]]]:
    with _STORE_THREAD_LOCK, _StoreFileLock(STORE_LOCK_PATH):
        return _read_store_unlocked()


def save_store(data: dict[str, list[dict[str, str]]]) -> None:
    with _STORE_THREAD_LOCK, _StoreFileLock(STORE_LOCK_PATH):
        _write_store_unlocked(data)


def reconcile_store_with_netbox(client: NetBoxClient) -> dict[str, Any]:
    """Drop local circuit_store entries that no longer exist in NetBox FMS.

    Field portal keeps a local draft store for multi-ODF registration, but the
    occupancy UI must follow NetBox after an explicit sync / NetBox-side delete.
    """
    circuits = paginate(client, "/plugins/fms/fiber-circuits/")
    netbox_cids = {
        (c.get("cid") or c.get("name") or "").strip()
        for c in circuits
        if (c.get("cid") or c.get("name") or "").strip()
    }

    removed: list[str] = []

    def _mut(store: dict[str, list[dict[str, str]]]) -> list[str]:
        drop = [cid for cid in list(store.keys()) if cid not in netbox_cids]
        for cid in drop:
            store.pop(cid, None)
            removed.append(cid)
        return drop

    dropped = mutate_store(_mut)
    if dropped:
        invalidate_after_circuit_change()
    return {
        "netbox_circuits": len(netbox_cids),
        "removed": removed,
        "removed_count": len(removed),
    }


def mutate_store(
    mutator: Callable[[dict[str, list[dict[str, str]]]], Any],
) -> Any:
    """Load → mutate → save under one exclusive lock (prevents lost updates)."""
    with _STORE_THREAD_LOCK, _StoreFileLock(STORE_LOCK_PATH):
        data = _read_store_unlocked()
        result = mutator(data)
        _write_store_unlocked(data)
        return result


def _conflict_message(odf: str, port: str, other_cid: str = "") -> str:
    who = f"（光路 {other_cid}）" if other_cid else ""
    return f"{odf} {port} 已被其他光路占用{who}。{_CONFLICT_HINT}"


def assert_submit_ports_available(
    client: NetBoxClient,
    config: dict[str, Any],
    *,
    cid: str,
    odf: str,
    port_local: str,
    peer: str,
    port_peer: str,
    cable: str,
    extra: list[tuple[str, str, str]] | None = None,
) -> None:
    """Fresh NetBox occupancy check at submit time (Plan A)."""
    web_base = config.get("web_base_url", config["base_url"].replace("/api", ""))
    checks = [
        (odf, port_local, cable),
        (peer, port_peer, cable),
    ]
    if extra:
        checks.extend(extra)
    seen: set[tuple[str, str]] = set()
    for dev, port, cab in checks:
        if not dev or not port:
            continue
        key = (dev, port)
        if key in seen:
            continue
        seen.add(key)
        other = _other_circuit_on_port(client, web_base, dev, port, cab, cid)
        if other:
            raise ValueError(_conflict_message(dev, port, other))


def _other_circuit_on_port(
    client: NetBoxClient,
    web_base: str,
    odf: str,
    port: str,
    cable: str,
    cid: str,
) -> str:
    """Return conflicting cid if port is taken by another circuit."""
    fp_id = _resolve_fp_id(client, odf, port, cable)
    if not fp_id:
        return ""
    entries = fp_index_for_targets(client, web_base, {fp_id}, force_refresh=False).get(fp_id, [])
    for entry in entries:
        ecid = (entry.get("cid") or "").strip()
        if ecid and ecid != cid:
            return ecid
    return ""


def paginate(client: NetBoxClient, path: str, params: dict | None = None) -> list[dict]:
    params = dict(params or {})
    params.setdefault("limit", 200)
    out: list[dict] = []
    while True:
        data = client.request("GET", path, params=params)
        out.extend(data.get("results", []))
        if not data.get("next"):
            break
        params["offset"] = params.get("offset", 0) + params["limit"]
    return out


def infer_cable_between(client: NetBoxClient, dev_a: str, dev_b: str) -> str:
    da = find_device(client, dev_a)
    db = find_device(client, dev_b)
    for cab in paginate(client, "/dcim/cables/"):
        dev_ids: set[int] = set()
        for term in (cab.get("a_terminations") or []) + (cab.get("b_terminations") or []):
            obj = term.get("object") or {}
            dev = obj.get("device") or {}
            if isinstance(dev, dict) and dev.get("id"):
                dev_ids.add(dev["id"])
        if {da["id"], db["id"]} <= dev_ids:
            lbl = cab.get("label") or ""
            if lbl:
                return lbl
    return ""


def cable_label_on_odf(client: NetBoxClient, odf: str, peer_odf: str, seg_cable: str = "") -> str:
    """Rear-port cable name as labeled on this ODF (not the peer's naming)."""
    dev = find_device(client, odf)
    labels: list[str] = []
    for rp in paginate(client, "/dcim/rear-ports/", {"device_id": dev["id"]}):
        lbl = (rp.get("name") or "").strip()
        if is_trunk_cable_label(lbl):
            labels.append(lbl)
    if peer_odf:
        for lbl in labels:
            if resolve_peer_from_cable(client, odf, lbl) == peer_odf:
                return lbl
    if seg_cable and seg_cable in labels:
        return seg_cable
    return labels[0] if labels else seg_cable


def is_odf_device(device: dict[str, Any]) -> bool:
    role = device.get("role") or device.get("device_role") or {}
    slug = (role.get("slug") or "").lower() if isinstance(role, dict) else ""
    if slug == "odf":
        return True
    from naming_rules import is_odf_device_name

    return is_odf_device_name(device.get("name") or "")


def list_odf_devices(client: NetBoxClient) -> list[dict[str, str]]:
    devices = paginate(client, "/dcim/devices/", {"role": "odf"})
    if not devices:
        devices = [d for d in paginate(client, "/dcim/devices/") if is_odf_device(d)]
    out: list[dict[str, str]] = []
    for d in devices:
        loc = d.get("location") or {}
        room = loc.get("name", "") if isinstance(loc, dict) else ""
        if not room:
            room = local_room_from_odf_name(d.get("name") or "")
        out.append(
            {
                "name": d["name"],
                "id": str(d["id"]),
                "site": (d.get("site") or {}).get("name", "") if isinstance(d.get("site"), dict) else "",
                "room": room,
            }
        )
    return sorted(out, key=lambda x: x["name"])


def default_fiber_options(strand_count: int = 12, *, cores_per_row: int = 12) -> list[str]:
    from naming_rules import fiber_from_position

    count = max(1, int(strand_count or cores_per_row))
    return [fiber_from_position(pos, cores_per_row=cores_per_row) for pos in range(1, count + 1)]


def list_odf_fibers(client: NetBoxClient, odf_name: str, cable_label: str = "") -> list[str]:
    from naming_rules import cable_label_variants, cable_port_prefix, fiber_from_port_name

    dev = find_device(client, odf_name)
    fibers: set[str] = set(default_fiber_options())
    cables_to_scan: list[str] = []
    local_room = local_room_from_odf_name(odf_name)
    if cable_label:
        cables_to_scan = cable_label_variants(cable_label)
    else:
        for rp in paginate(client, "/dcim/rear-ports/", {"device_id": dev["id"]}):
            lbl = (rp.get("name") or "").strip()
            if is_trunk_cable_label(lbl):
                cables_to_scan.append(lbl)
        cables_to_scan = sorted(set(cables_to_scan))
    for cab in cables_to_scan:
        pfx = cable_port_prefix(cab, local_room)
        for fp in paginate(client, "/dcim/front-ports/", {"device_id": dev["id"]}):
            name = fp.get("name") or ""
            if not name.startswith(pfx):
                continue
            fb = fiber_from_port_name(name, cab, local_room=local_room)
            if fb:
                fibers.add(fb)
    store = load_store()
    for segs in store.values():
        for s in segs:
            if s.get("dev_a") == odf_name:
                fibers.add(s["port_a"])
            if s.get("dev_b") == odf_name:
                fibers.add(s["port_b"])
    return sorted(fibers, key=fiber_sort_key)


def _cables_by_device_id(client: NetBoxClient) -> dict[int, list[dict[str, str]]]:
    """Index CBL-* cables by device id (cached 1 hour)."""
    now = time.time()
    cache = getattr(_cables_by_device_id, "_cache", None)
    if cache and now - cache.get("ts", 0) < 3600:
        return cache["data"]

    by_dev: dict[int, list[dict[str, str]]] = defaultdict(list)
    seen: dict[int, set[str]] = defaultdict(set)
    for cab in paginate(client, "/dcim/cables/"):
        lbl = (cab.get("label") or "").strip()
        if not lbl or not is_trunk_cable_label(lbl):
            continue
        devices: dict[int, str] = {}
        for term in (cab.get("a_terminations") or []) + (cab.get("b_terminations") or []):
            obj = term.get("object") or {}
            d = obj.get("device") or {}
            if isinstance(d, dict) and d.get("id"):
                devices[int(d["id"])] = d.get("name") or ""
        for did, odf_name in devices.items():
            peers = sorted(set(devices.values()) - {odf_name})
            peer_val = peers[0] if len(peers) == 1 else ""
            for alias in cable_label_variants(lbl):
                if alias in seen[did]:
                    continue
                seen[did].add(alias)
                by_dev[did].append({"label": alias, "peer": peer_val})
    out = {did: sorted(rows, key=lambda x: x["label"]) for did, rows in by_dev.items()}
    _cables_by_device_id._cache = {"ts": now, "data": out}
    return out


def cables_at_odf(client: NetBoxClient, odf_name: str) -> list[dict[str, str]]:
    dev = find_device(client, odf_name)
    return list(_cables_by_device_id(client).get(dev["id"], []))


def make_cid(business_name: str) -> str:
    name = (business_name or "").strip()
    if not name:
        return ""
    if name.upper().startswith("FIB-"):
        return name
    safe = re.sub(r"\s+", "-", name)
    safe = re.sub(r"[^\w\-一-龥]", "", safe)
    return f"FIB-{safe}"


def _peer_from_rear_ports(client: NetBoxClient, odf_name: str, cable_label: str) -> str:
    """Peer ODF from rear-port link_peers / attached DCIM cable."""
    variants = set(cable_label_variants(cable_label)) | set(cable_label_variants(odf_name))
    variants.discard("")
    if not variants:
        return ""
    try:
        did = find_device(client, odf_name)["id"]
    except Exception:
        return ""
    for rp in paginate(client, "/dcim/rear-ports/", {"device_id": did}):
        lbl = (rp.get("name") or "").strip()
        if lbl not in variants:
            continue
        for peer in rp.get("link_peers") or []:
            if not isinstance(peer, dict):
                continue
            dev = peer.get("device") or {}
            name = (dev.get("name") or "").strip() if isinstance(dev, dict) else ""
            if name and name != odf_name:
                return name
        cab = rp.get("cable")
        cab_id = cab.get("id") if isinstance(cab, dict) else None
        if not cab_id:
            continue
        try:
            detail = client.request("GET", f"/dcim/cables/{int(cab_id)}/")
        except Exception:
            continue
        others: list[str] = []
        for term in (detail.get("a_terminations") or []) + (detail.get("b_terminations") or []):
            obj = term.get("object") or {}
            d = obj.get("device") or {}
            name = (d.get("name") or "").strip() if isinstance(d, dict) else ""
            if name and name != odf_name:
                others.append(name)
        if len(set(others)) == 1:
            return others[0]
    return ""


def _peer_odf_from_route_name(client: NetBoxClient, odf_name: str, cable_label: str = "") -> str:
    """Route-named ODF 得丰机房至2号门对面电箱机房 → peer device 2号门对面电箱机房至得丰机房."""
    candidates: list[str] = []
    for src in (cable_label, odf_name):
        mirrored = mirror_cable_label(src or "")
        if mirrored and mirrored != src:
            candidates.append(mirrored)
    local = local_room_from_odf_name(odf_name)
    peer_room = peer_room_from_odf_name(odf_name)
    if not peer_room and cable_label:
        p = parse_route_label(cable_label)
        if p.get("room_a") and p.get("room_b"):
            ra, rb = normalize_room_name(p["room_a"]), normalize_room_name(p["room_b"])
            if _rooms_match(local, ra):
                peer_room = rb
            elif _rooms_match(local, rb):
                peer_room = ra
            else:
                peer_room = rb
    if local and peer_room:
        candidates.append(format_odf_device_name(peer_room, local))
    seen: set[str] = set()
    for name in candidates:
        name = (name or "").strip()
        if not name or name in seen or name == odf_name:
            continue
        seen.add(name)
        try:
            find_device(client, name)
            return name
        except Exception:
            continue
    if not peer_room:
        return ""
    try:
        from field_browser import list_all_odf_records

        records = list_all_odf_records(client)
    except Exception:
        records = list_odf_devices(client)
    for rec in records:
        name = (rec.get("name") or "").strip()
        if not name or name == odf_name:
            continue
        if not _rooms_match(local_room_from_odf_name(name), peer_room):
            continue
        other_peer = peer_room_from_odf_name(name)
        if not other_peer or _rooms_match(other_peer, local):
            return name
    return ""


def resolve_peer_from_cable(client: NetBoxClient, odf_name: str, cable_label: str) -> str:
    cable_label = (cable_label or "").strip()
    odf_name = (odf_name or "").strip()
    cache_key = f"{odf_name}|{cable_label}"
    now = time.time()
    with _DISPLAY_CACHE_LOCK:
        hit = _PEER_RESOLVE_MEM.get(cache_key)
        if hit and now - hit[0] < _PEER_RESOLVE_TTL_SEC:
            return hit[1]
    peer = _resolve_peer_from_cable_uncached(client, odf_name, cable_label)
    with _DISPLAY_CACHE_LOCK:
        _PEER_RESOLVE_MEM[cache_key] = (now, peer)
    return peer


def _resolve_peer_from_cable_uncached(client: NetBoxClient, odf_name: str, cable_label: str) -> str:
    if cable_label and odf_name:
        variants = set(cable_label_variants(cable_label))
        for c in cables_at_odf(client, odf_name):
            if c["label"] in variants and c.get("peer"):
                return c["peer"]
        named = _peer_odf_from_route_name(client, odf_name, cable_label)
        if named:
            return named
        try:
            did = find_device(client, odf_name)["id"]
        except Exception:
            did = None
        if did:
            for variant in variants:
                hits = client.request("GET", "/dcim/cables/", params={"label": variant, "limit": 20})
                for cab in hits.get("results", []):
                    peers: set[str] = set()
                    hit = False
                    for term in (cab.get("a_terminations") or []) + (cab.get("b_terminations") or []):
                        obj = term.get("object") or {}
                        d = obj.get("device") or {}
                        if isinstance(d, dict) and d.get("id") == did:
                            hit = True
                        if isinstance(d, dict) and d.get("name"):
                            peers.add(d["name"])
                    if hit:
                        others = sorted(peers - {odf_name})
                        if len(others) == 1:
                            return others[0]
    if odf_name:
        via_rear = _peer_from_rear_ports(client, odf_name, cable_label or odf_name)
        if via_rear:
            return via_rear
        return _peer_odf_from_route_name(client, odf_name, cable_label)
    return ""


def list_local_odf_options(client: NetBoxClient, odf_name: str, *, same_room_only: bool = True) -> list[dict[str, Any]]:
    return list_jump_target_options(client, odf_name, current_cable="", same_room_only=same_room_only)


def list_jump_target_options(
    client: NetBoxClient,
    odf_name: str,
    current_cable: str = "",
    *,
    same_room_only: bool = True,
) -> list[dict[str, Any]]:
    """Jump targets: other routes on same ODF frame + other ODFs in the same room."""
    from field_browser import build_odf_route_entries, list_all_odf_records, _rear_ports_by_device_id
    from portal_settings import apply_route_policy

    dev = find_device(client, odf_name)
    loc = dev.get("location") or {}
    room = (loc.get("name", "") if isinstance(loc, dict) else "") or _room_from_odf_name(odf_name)

    records = list_all_odf_records(client)
    if same_room_only:
        records = [r for r in records if r.get("room") == room]
    rear_by_dev = _rear_ports_by_device_id(client)
    entries = apply_route_policy(build_odf_route_entries(client, records, rear_by_dev=rear_by_dev, light=True))

    out: list[dict[str, Any]] = []
    current_vars = set(cable_label_variants(current_cable)) if current_cable else set()
    for entry in entries:
        name = entry.get("name") or ""
        cab = entry.get("cable") or ""
        if name == odf_name and cab and (cab in current_vars or cab == current_cable):
            continue
        same_frame = name == odf_name
        rk = entry.get("route_key") or f"{name}|{cab}"
        out.append(
            {
                "name": name,
                "jump_odf": name,
                "jump_cable": cab,
                "display_name": entry.get("display_name") or name,
                "route_key": rk,
                "same_room": True,
                "same_frame": same_frame,
            }
        )
    out.sort(key=lambda x: (not x["same_frame"], x.get("display_name") or ""))
    return out


def list_room_devices(client: NetBoxClient, odf_name: str) -> list[dict[str, str]]:
    """Non-ODF devices in the same room as the ODF (switches, transceivers)."""
    dev = find_device(client, odf_name)
    loc = dev.get("location") or {}
    room_name = (loc.get("name", "") if isinstance(loc, dict) else "") or _room_from_odf_name(odf_name)
    room_short = room_name.replace("机房", "").strip() if room_name else ""
    site = dev.get("site") or {}
    site_id = site.get("id") if isinstance(site, dict) else site
    params: dict[str, Any] = {"limit": 200}
    if site_id:
        params["site_id"] = site_id
    out: list[dict[str, str]] = []
    for d in paginate(client, "/dcim/devices/", params):
        name = d.get("name") or ""
        if not name or is_odf_device(d):
            continue
        dloc = (d.get("location") or {}).get("name", "") if isinstance(d.get("location"), dict) else ""
        in_room = bool(room_name and (dloc == room_name or room_name in name))
        if not in_room and room_short:
            in_room = room_short in name or name.startswith(room_short)
        if room_name and not in_room:
            continue
        role = d.get("device_role") or {}
        out.append(
            {
                "name": name,
                "role": role.get("name", "") if isinstance(role, dict) else "",
                "location": dloc or room_name,
            }
        )
    return sorted(out, key=lambda x: x["name"])


def list_site_devices(client: NetBoxClient, odf_name: str) -> list[dict[str, str]]:
    dev = find_device(client, odf_name)
    site = dev.get("site") or {}
    site_id = site.get("id") if isinstance(site, dict) else site
    params: dict[str, Any] = {"limit": 200}
    if site_id:
        params["site_id"] = site_id
    out: list[dict[str, str]] = []
    for d in paginate(client, "/dcim/devices/", params):
        name = d.get("name") or ""
        if not name or is_odf_device(d):
            continue
        role = d.get("device_role") or {}
        out.append(
            {
                "name": name,
                "role": role.get("name", "") if isinstance(role, dict) else "",
                "location": (d.get("location") or {}).get("name", "") if isinstance(d.get("location"), dict) else "",
            }
        )
    return sorted(out, key=lambda x: x["name"])


def list_device_interfaces(client: NetBoxClient, device_name: str) -> list[dict[str, str]]:
    dev = find_device(client, device_name)
    out: list[dict[str, str]] = []
    for iface in paginate(client, "/dcim/interfaces/", {"device_id": dev["id"]}):
        name = iface.get("name") or ""
        if not name:
            continue
        typ = iface.get("type") or {}
        type_label = typ.get("label", "") if isinstance(typ, dict) else str(typ)
        out.append({"name": name, "type": type_label, "enabled": str(iface.get("enabled", True))})
    fiberish = [i for i in out if any(k in (i["type"] + i["name"]).lower() for k in ("fiber", "sfp", "光", "xe", "ge"))]
    return fiberish or out


def describe_front_port_local_from_fp(
    fp: dict[str, Any],
    *,
    client: NetBoxClient | None = None,
    odf_name: str = "",
    cable_label: str = "",
) -> dict[str, Any]:
    targets: list[dict[str, str]] = []
    for link_peer in fp.get("link_peers") or []:
        if not isinstance(link_peer, dict):
            continue
        dev = link_peer.get("device") or {}
        dev_name = dev.get("name", "") if isinstance(dev, dict) else ""
        pn = link_peer.get("name") or ""
        kind = "odf" if "ODF" in dev_name.upper() else "device"
        targets.append({"device": dev_name, "port": pn, "kind": kind})
    if targets:
        t0 = targets[0]
        pf = fiber_from_port_name(t0["port"]) or t0["port"]
        if t0["kind"] == "odf":
            if client and t0["device"] == odf_name:
                tgt_cab = cable_for_port_on_odf(client, odf_name, t0["port"])
                tgt_label = route_display_name(client, odf_name, tgt_cab)
                summary = f"跳纤 → 同框 · {tgt_label} {pf}"
            elif client:
                tgt_cab = cable_for_port_on_odf(client, t0["device"], t0["port"])
                summary = f"跳纤 → {peer_route_display_name(client, t0['device'], tgt_cab or cable_label)} {pf}"
            else:
                summary = f"跳纤 → {t0['device']} {t0['port']}"
            return {"local_type": "jump", "targets": targets, "summary": summary}
        disp = t0["device"]
        if client:
            disp = route_display_name(client, t0["device"]) if "ODF" in t0["device"].upper() else t0["device"]
        return {"local_type": "device", "targets": targets, "summary": f"直连 → {disp} {pf}"}
    cable = fp.get("cable")
    if isinstance(cable, dict) and cable.get("label", "").startswith(("PATCH-", "LINK-")):
        return {"local_type": "jump", "targets": targets, "summary": cable.get("label", "")}
    return {"local_type": "empty", "targets": [], "summary": "空闲（仅缆段侧）"}


def describe_front_port_local(
    client: NetBoxClient,
    fp_id: int | None,
    *,
    odf_name: str = "",
    cable_label: str = "",
) -> dict[str, Any]:
    if not fp_id:
        return {"local_type": "empty", "targets": [], "summary": ""}
    fp = client.request("GET", f"/dcim/front-ports/{fp_id}/")
    return describe_front_port_local_from_fp(fp, client=client, odf_name=odf_name, cable_label=cable_label)


def apply_local_extension(client: NetBoxClient, payload: dict[str, Any]) -> dict[str, Any] | None:
    local_type = (payload.get("local_type") or "trunk_only").strip().lower()
    if local_type in ("trunk_only", "none", ""):
        return None

    odf = (payload.get("odf") or "").strip()
    port_local = (payload.get("port_local") or "").strip()
    cable = (payload.get("cable") or "").strip()
    cid = (payload.get("cid") or "").strip()
    infra = FiberInfra(client)

    if local_type == "jump_odf":
        jump_odf = (payload.get("jump_odf") or "").strip()
        jump_fiber = (payload.get("jump_fiber") or port_local).strip()
        jump_cable = (payload.get("jump_cable") or "").strip()
        if not jump_odf:
            raise ValueError("请选择跳纤目标 ODF")
        if jump_odf == odf and jump_fiber != port_local:
            cab_a = (cable or "").strip()
            cab_b = (jump_cable or "").strip()
            same_cable = not cab_b or not cab_a or cab_b == cab_a
            if not same_cable:
                a_vars = set(cable_label_variants(cab_a))
                same_cable = cab_b in a_vars
            if same_cable:
                raise ValueError(
                    f"同缆段跳纤两端纤芯须相同（本端 {port_local}，目标 {jump_fiber}）。"
                    " 换缆跳纤请选择另一缆段路由并指定目标纤芯（如 1-1→1-3）。"
                )
        infra.apply_port_links(
            [
                {
                    "dev_a": odf,
                    "port_a": port_local,
                    "cab_a": cable,
                    "dev_b": jump_odf,
                    "port_b": jump_fiber,
                    "cab_b": jump_cable,
                    "note": f"现场跳纤 {cid}".strip(),
                    "cid": cid,
                }
            ]
        )
        return {"local_type": "jump_odf", "target": jump_odf, "fiber": jump_fiber, "cable": jump_cable}

    if local_type == "to_device":
        device = (payload.get("device") or "").strip()
        device_port = (payload.get("device_port") or "").strip()
        if not device:
            raise ValueError("请选择目标设备")
        dev_odf = find_device(client, odf)
        fp = find_front_port(client, dev_odf["id"], resolve_port_name(port_local, cable, device_name=odf), port_local)
        target = find_device(client, device)
        interfaces = paginate(client, "/dcim/interfaces/", {"device_id": target["id"]})
        iface = next((i for i in interfaces if i["name"] == device_port), None)
        if not iface and device_port:
            iface = next((i for i in interfaces if device_port in i["name"]), None)
        if not iface and interfaces:
            iface = interfaces[0]
        if not iface:
            raise ValueError(f"设备 {device} 无可用接口，请先在 NetBox 创建设备接口")
        label = re.sub(r"[^\w\-]", "-", f"LINK-{odf}-{port_local}-{device}-{iface['name']}")[:100]
        hits = client.request("GET", "/dcim/cables/", params={"label": label, "limit": 1})
        if not hits.get("results"):
            client.request(
                "POST",
                "/dcim/cables/",
                json={
                    "type": "smf",
                    "status": "connected",
                    "label": label,
                    "a_terminations": [{"object_type": "dcim.frontport", "object_id": fp["id"]}],
                    "b_terminations": [{"object_type": "dcim.interface", "object_id": iface["id"]}],
                },
            )
        return {"local_type": "to_device", "device": device, "interface": iface["name"]}

    raise ValueError(f"未知本端类型: {local_type}")


def _submit_extension_cable_ids(client: NetBoxClient, payload: dict[str, Any]) -> set[int]:
    """DCIM cable ids currently implementing this submit's Front extension."""
    local_type = (payload.get("local_type") or "trunk_only").strip().lower()
    ids: set[int] = set()
    odf = (payload.get("odf") or "").strip()
    port_local = (payload.get("port_local") or "").strip()
    cable = (payload.get("cable") or "").strip()
    if local_type == "jump_odf":
        jump_odf = (payload.get("jump_odf") or "").strip()
        jump_fiber = (payload.get("jump_fiber") or port_local).strip()
        jump_cable = (payload.get("jump_cable") or "").strip()
        fp_a = _resolve_fp_id(client, odf, port_local, cable)
        fp_b = _resolve_fp_id(client, jump_odf, jump_fiber, jump_cable)
        cab_id = _find_front_port_link_cable_id(client, int(fp_a or 0), int(fp_b or 0))
        if cab_id:
            ids.add(int(cab_id))
        return ids
    if local_type == "to_device":
        device = (payload.get("device") or "").strip()
        device_port = (payload.get("device_port") or "").strip()
        if not device or not device_port:
            return ids
        label = re.sub(r"[^\w\-]", "-", f"LINK-{odf}-{port_local}-{device}-{device_port}")[:100]
        hits = client.request("GET", "/dcim/cables/", params={"label": label, "limit": 5})
        for row in hits.get("results") or []:
            if row.get("id"):
                ids.add(int(row["id"]))
    return ids


def _delete_dcim_cables_best_effort(client: NetBoxClient, cable_ids: set[int]) -> None:
    if not cable_ids:
        return
    infra = FiberInfra(client)
    for cab_id in cable_ids:
        try:
            infra.delete_cable_resolving_deps(int(cab_id))
        except Exception:
            try:
                client.request("DELETE", f"/dcim/cables/{int(cab_id)}/")
            except Exception:
                pass


def neighbor_odfs(client: NetBoxClient, odf_name: str) -> list[str]:
    peers: set[str] = set()
    for c in cables_at_odf(client, odf_name):
        if c.get("peer"):
            peers.add(c["peer"])
    return sorted(peers)


def get_odf_context(client: NetBoxClient, odf_name: str, web_base: str, *, fp_index: dict | None = None, cable_filter: str = "") -> dict[str, Any]:
    dev = find_device(client, odf_name)
    neighbors = neighbor_odfs(client, odf_name)
    fp_index = fp_index or build_fp_circuit_index(client, web_base)
    dev_fps = paginate(client, "/dcim/front-ports/", {"device_id": dev["id"]})
    relevant_cids: set[str] = set()
    for fp in dev_fps:
        for c in fp_index.get(fp["id"], []):
            if c.get("cid"):
                relevant_cids.add(c["cid"])

    peer_options = [{"name": n, "is_neighbor": True} for n in neighbors]
    peer_options.sort(key=lambda x: x["name"])

    cables = cables_at_odf(client, odf_name)
    for peer in neighbors:
        lbl = infer_cable_between(client, odf_name, peer)
        if lbl and not any(c["label"] == lbl for c in cables):
            cables.append({"label": lbl, "peer": peer})
    cables = sorted(cables, key=lambda x: x["label"])

    fibers = list_odf_fibers(client, odf_name, cable_filter)

    store = load_store()
    pending: list[dict[str, Any]] = []
    for cid, segs in store.items():
        if not segs:
            continue
        pending.append({"cid": cid, "segments": len(segs), "route": build_route_short(segs)})

    local_pending: list[dict[str, Any]] = []
    for cid, segs in store.items():
        for s in segs:
            if s.get("dev_a") == odf_name or s.get("dev_b") == odf_name:
                local_pending.append({"cid": cid, "segments": len(segs), "route": build_route_short(segs)})
                break

    circuits = paginate(client, "/plugins/fms/fiber-circuits/")
    existing = [
        {
            "cid": c.get("cid") or c.get("name"),
            "name": c.get("name") or c.get("cid"),
            "id": c["id"],
        }
        for c in circuits
        if (c.get("cid") or c.get("name"))
    ]
    local_cids = {p["cid"] for p in local_pending}
    seen: set[str] = set()
    circuit_options: list[dict[str, Any]] = []
    for e in existing:
        cid = (e.get("cid") or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        row = dict(e)
        if cid in relevant_cids:
            row["priority"] = 0
        elif cid in local_cids:
            row["priority"] = 1
        else:
            row["priority"] = 2
        circuit_options.append(row)
    for p in pending:
        cid = (p.get("cid") or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        circuit_options.append(
            {
                "cid": cid,
                "name": cid,
                "id": None,
                "pending": True,
                "route": p.get("route", ""),
                "priority": 0 if cid in local_cids else 1,
            }
        )
    circuit_options.sort(key=lambda x: (x.get("priority", 9), x.get("cid") or ""))

    return {
        "odf": odf_name,
        "site": (dev.get("site") or {}).get("name", ""),
        "neighbors": neighbors,
        "peer_options": peer_options,
        "cables": cables,
        "fibers": fibers,
        "roles": [
            {"value": "a", "label": "本框为 A 端（起点侧）"},
            {"value": "b", "label": "本框为 B 端（终点侧）"},
        ],
        "wavelengths": ["1310", "1550"],
        "pending_circuits": local_pending,
        "existing_circuits": [e for e in existing if e["cid"] in relevant_cids],
        "circuit_options": circuit_options,
        "device_url": f"{web_base.rstrip('/')}/dcim/devices/{dev['id']}/",
        "local_odf_options": list_jump_target_options(client, odf_name, cable_filter, same_room_only=True),
        "jump_target_options": list_jump_target_options(client, odf_name, cable_filter, same_room_only=True),
        "room_devices": list_room_devices(client, odf_name),
        "site_devices": list_site_devices(client, odf_name),
        "local_types": [
            {"value": "trunk_only", "label": "仅登记缆段（中间跳/暂无延伸）"},
            {"value": "jump_odf", "label": "跳纤到其他缆段/ODF框"},
            {"value": "to_device", "label": "直连交换机/光模块"},
        ],
    }


def segment_from_field(
    odf: str,
    peer_odf: str,
    role: str,
    port_local: str,
    port_peer: str,
    *,
    cid: str,
    cable: str = "",
    name: str = "",
    service: str = "",
    ip: str = "",
    loss_db: str = "",
) -> dict[str, str]:
    from naming_rules import normalize_fiber_label

    role = (role or "a").lower()
    port_local = normalize_fiber_label(port_local)
    port_peer = normalize_fiber_label(port_peer)
    if role == "b":
        dev_a, port_a, dev_b, port_b = peer_odf, port_peer, odf, port_local
    else:
        dev_a, port_a, dev_b, port_b = odf, port_local, peer_odf, port_peer
    return {
        "cid": cid.strip(),
        "dev_a": dev_a,
        "port_a": port_a,
        "dev_b": dev_b,
        "port_b": port_b,
        "cable": cable.strip(),
        "name": name.strip() or cid.strip(),
        "service": service.strip(),
        "ip": ip.strip(),
        "loss_db": loss_db.strip(),
        "label_pos": f"{odf} {port_local} -> {peer_odf} {port_peer}",
    }


def dedupe_segment_rows(segments: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple] = set()
    out: list[dict[str, str]] = []
    for seg in segments:
        key = (seg.get("dev_a", ""), seg.get("port_a", ""), seg.get("dev_b", ""), seg.get("port_b", ""), seg.get("cable") or "")
        rev = (seg.get("dev_b", ""), seg.get("port_b", ""), seg.get("dev_a", ""), seg.get("port_a", ""), seg.get("cable") or "")
        if key in seen or rev in seen:
            continue
        seen.add(key)
        seen.add(rev)
        out.append(seg)
    return out


def segment_is_reverse_of_existing(existing: list[dict[str, str]], seg: dict[str, str]) -> bool:
    for s in existing:
        if (s.get("cable") or "") != (seg.get("cable") or ""):
            continue
        if (
            s.get("dev_a") == seg.get("dev_b")
            and s.get("dev_b") == seg.get("dev_a")
            and s.get("port_a") == seg.get("port_b")
            and s.get("port_b") == seg.get("port_a")
        ):
            return True
    return False


def resolve_segment_role(
    odf: str,
    port_local: str,
    existing: list[dict[str, str]],
    *,
    default: str = "a",
) -> str:
    from import_from_excel import auto_order_segments

    if not existing:
        return default
    ordered = auto_order_segments(list(existing))
    if not ordered:
        return default
    last, first = ordered[-1], ordered[0]
    if last.get("dev_b") == odf and last.get("port_b") == port_local:
        return "a"
    if first.get("dev_a") == odf and first.get("port_a") == port_local:
        return "b"
    return default


def trace_covers_registered_segments(
    client: NetBoxClient,
    path_id: int,
    segs: list[dict[str, str]],
    *,
    web_base: str = "",
) -> bool:
    if not segs:
        return True
    try:
        base = web_base or getattr(client, "base_url", "").replace("/api", "")
        trace = get_fms_path_trace(client, path_id, base, force_refresh=True)
    except Exception:
        return False
    if _registered_endpoints_in_trace(trace, segs, client):
        return True
    needed = {(s.get("cable") or "").strip() for s in segs if (s.get("cable") or "").strip()}
    if not needed:
        return False
    cables_in_trace = {h.get("label") for h in trace.get("hops") or [] if h.get("type") == "cable"}
    mirror_needed = set()
    for cab in needed:
        mirror_needed.add(cab)
        m = mirror_cable_label(cab)
        if m:
            mirror_needed.add(m)
    return needed <= cables_in_trace or mirror_needed <= cables_in_trace


def _find_rear_port_id(client: NetBoxClient, device_id: int, cable_label: str) -> int | None:
    for variant in cable_label_variants(cable_label):
        hits = client.request("GET", "/dcim/rear-ports/", params={"device_id": device_id, "name": variant, "limit": 1})
        if hits.get("results"):
            return int(hits["results"][0]["id"])
    return None


def _find_dcim_cable_id(client: NetBoxClient, cable_label: str) -> int | None:
    for variant in cable_label_variants(cable_label):
        hits = client.request("GET", "/dcim/cables/", params={"label": variant, "limit": 1})
        if hits.get("results"):
            return int(hits["results"][0]["id"])
    return None


def _resolve_segment_fp_id(client: NetBoxClient, dev: str, port: str, cable: str) -> int:
    dev_obj = find_device(client, dev)
    pname = resolve_port_name(port, cable, device_name=dev)
    fp = find_front_port(client, dev_obj["id"], pname, port)
    return int(fp["id"])


def _is_jump_segment_row(seg: dict[str, str]) -> bool:
    from naming_rules import cable_belongs_on_route_odf

    da = (seg.get("dev_a") or "").strip()
    cab = (seg.get("cable") or "").strip()
    return "(跳纤段)" in (seg.get("label_pos") or "") or (da and cab and not cable_belongs_on_route_odf(da, cab))


def _trunk_dcim_hops(client: NetBoxClient, dev_a: str, cab_a: str, dev_b: str, cab_b: str) -> list[dict[str, Any]]:
    da = find_device(client, dev_a)
    db = find_device(client, dev_b)
    rp_a = _find_rear_port_id(client, da["id"], cab_a)
    rp_b = _find_rear_port_id(client, db["id"], cab_b)
    cab_id = _find_dcim_cable_id(client, cab_a) or _find_dcim_cable_id(client, cab_b)
    hops: list[dict[str, Any]] = []
    if rp_a:
        hops.append({"type": "rear_port", "id": rp_a})
    if cab_id:
        hops.append({"type": "cable", "id": cab_id})
    if rp_b:
        hops.append({"type": "rear_port", "id": rp_b})
    return hops


def _append_front_port(nodes: list[dict[str, Any]], fp_id: int) -> None:
    if fp_id and (not nodes or nodes[-1].get("id") != fp_id):
        nodes.append({"type": "front_port", "id": fp_id})


def _append_port_link(nodes: list[dict[str, Any]], cable_id: int | None) -> None:
    if not cable_id:
        return
    if nodes and nodes[-1].get("type") == "port_link" and nodes[-1].get("id") == cable_id:
        return
    nodes.append({"type": "port_link", "id": int(cable_id)})


def _find_front_port_link_cable_id(client: NetBoxClient, fp_a: int, fp_b: int) -> int | None:
    """Return DCIM cable id linking two front ports (PATCH-/LINK- jump), if any."""
    if not fp_a or not fp_b:
        return None
    try:
        fp = client.request("GET", f"/dcim/front-ports/{int(fp_a)}/")
    except Exception:
        return None
    for peer in fp.get("link_peers") or []:
        if int(peer.get("id") or 0) != int(fp_b):
            continue
        cab = peer.get("cable") or fp.get("cable")
        if isinstance(cab, dict) and cab.get("id"):
            return int(cab["id"])
    cab = fp.get("cable")
    if isinstance(cab, dict) and cab.get("id"):
        # Some NetBox versions only expose cable on the port, not peers.
        try:
            detail = client.request("GET", f"/dcim/cables/{int(cab['id'])}/")
            terms = []
            for side in ("a_terminations", "b_terminations"):
                for t in detail.get(side) or []:
                    if t.get("object_type") == "dcim.frontport" and t.get("object_id"):
                        terms.append(int(t["object_id"]))
            if int(fp_a) in terms and int(fp_b) in terms:
                return int(cab["id"])
        except Exception:
            pass
    return None


def _flip_segment_row(seg: dict[str, str]) -> dict[str, str]:
    out = dict(seg)
    out["dev_a"], out["dev_b"] = (seg.get("dev_b") or "").strip(), (seg.get("dev_a") or "").strip()
    out["port_a"], out["port_b"] = (seg.get("port_b") or "").strip(), (seg.get("port_a") or "").strip()
    cab_a = (seg.get("cab_a") or seg.get("cable") or "").strip()
    cab_b = (seg.get("cab_b") or seg.get("cable") or "").strip()
    out["cab_a"], out["cab_b"] = cab_b, cab_a
    if _is_jump_segment_row(seg):
        out["label_pos"] = f'{out["dev_a"]} {out["port_a"]} -> {out["dev_b"]} {out["port_b"]} (跳纤段)'
    else:
        # Keep a usable trunk cable label after flip.
        out["cable"] = cab_b or cab_a or (seg.get("cable") or "").strip()
        out["label_pos"] = f'{out["dev_a"]} {out["port_a"]} -> {out["dev_b"]} {out["port_b"]}'
    return out


def _orient_segments_for_explicit_path(segs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Orient store rows into one continuous A→B chain (avoid go-out-and-back trunks)."""
    if not segs:
        return []
    remaining = [dict(s) for s in segs]
    if len(remaining) == 1:
        return remaining

    counts: Counter[str] = Counter()
    for s in remaining:
        da = (s.get("dev_a") or "").strip()
        db = (s.get("dev_b") or "").strip()
        if da:
            counts[da] += 1
        if db:
            counts[db] += 1
    leaves = [d for d, n in counts.items() if n == 1]

    def _take_start() -> dict[str, str] | None:
        # Prefer a leaf endpoint so the path starts at a real terminal ODF.
        for leaf in leaves:
            for i, s in enumerate(remaining):
                da = (s.get("dev_a") or "").strip()
                db = (s.get("dev_b") or "").strip()
                if da == leaf:
                    return remaining.pop(i)
                if db == leaf:
                    return _flip_segment_row(remaining.pop(i))
        return remaining.pop(0)

    ordered: list[dict[str, str]] = []
    cur = _take_start()
    if not cur:
        return [dict(s) for s in segs]
    ordered.append(cur)
    while remaining:
        cur_dev = (ordered[-1].get("dev_b") or "").strip()
        cur_port = (ordered[-1].get("port_b") or "").strip()
        match_i = None
        match_flip = False
        # Exact port continuity first, then device-only.
        for prefer_port in (True, False):
            for i, s in enumerate(remaining):
                da = (s.get("dev_a") or "").strip()
                db = (s.get("dev_b") or "").strip()
                pa = (s.get("port_a") or "").strip()
                pb = (s.get("port_b") or "").strip()
                if da == cur_dev and (not prefer_port or pa == cur_port):
                    match_i, match_flip = i, False
                    break
                if db == cur_dev and (not prefer_port or pb == cur_port):
                    match_i, match_flip = i, True
                    break
            if match_i is not None:
                break
        if match_i is None:
            # Cannot extend — append leftovers as-is (better than dropping).
            ordered.extend(remaining)
            break
        nxt = remaining.pop(match_i)
        ordered.append(_flip_segment_row(nxt) if match_flip else nxt)
    return ordered


def build_explicit_fms_path(client: NetBoxClient, segs: list[dict[str, str]]) -> tuple[list[dict[str, Any]], int, int]:
    """Build FMS path nodes from portal registration (retrace alone misses cross-fiber + jump)."""
    nodes: list[dict[str, Any]] = []
    for seg in _orient_segments_for_explicit_path(segs):
        cab_seg = (seg.get("cable") or "").strip()
        cab_a = (seg.get("cab_a") or cab_seg).strip()
        cab_b = (seg.get("cab_b") or cab_seg).strip()
        da = (seg.get("dev_a") or "").strip()
        db = (seg.get("dev_b") or "").strip()
        pa = (seg.get("port_a") or "").strip()
        pb = (seg.get("port_b") or "").strip()
        fp_a = _resolve_segment_fp_id(client, da, pa, cab_a)
        fp_b = _resolve_segment_fp_id(client, db, pb, cab_b)

        # Already standing on this front port from the previous segment — don't loop back.
        if not (nodes and nodes[-1].get("type") == "front_port" and int(nodes[-1].get("id") or 0) == int(fp_a)):
            _append_front_port(nodes, fp_a)

        if _is_jump_segment_row(seg):
            _append_port_link(nodes, _find_front_port_link_cable_id(client, fp_a, fp_b))
            _append_front_port(nodes, fp_b)
            continue

        nodes.extend(_trunk_dcim_hops(client, da, cab_a, db, cab_b))
        if pa != pb:
            fp_land = _resolve_segment_fp_id(client, db, pa, cab_b)
            _append_front_port(nodes, fp_land)
            _append_port_link(nodes, _find_front_port_link_cable_id(client, fp_land, fp_b))
            _append_front_port(nodes, fp_b)
        else:
            _append_front_port(nodes, fp_b)

    if not nodes:
        raise ValueError("无法构建 FMS 路径节点")
    return nodes, int(nodes[0]["id"]), int(nodes[-1]["id"])
def upsert_segment(store: dict[str, list[dict[str, str]]], seg: dict[str, str]) -> None:
    cid = seg["cid"]
    segs = store.setdefault(cid, [])
    key = (seg["dev_a"], seg["dev_b"], seg["port_a"], seg["port_b"])
    rev_key = (seg["dev_b"], seg["dev_a"], seg["port_b"], seg["port_a"])
    segs[:] = [
        s
        for s in segs
        if (s["dev_a"], s["dev_b"], s["port_a"], s["port_b"]) not in (key, rev_key)
    ]
    segs.append(seg)


def port_in_circuit_segments(segments: list[dict[str, str]], odf: str, port_local: str) -> bool:
    for seg in segments:
        if seg["dev_a"] == odf and seg["port_a"] == port_local:
            return True
        if seg["dev_b"] == odf and seg["port_b"] == port_local:
            return True
    return False


def should_skip_segment_upsert(
    *,
    mode: str,
    local_type: str,
    segments: list[dict[str, str]],
    odf: str,
    port_local: str,
) -> bool:
    """Continue on an in-use port with jump/device only — do not add a reverse trunk row."""
    if mode != "continue":
        return False
    if local_type not in ("jump_odf", "to_device"):
        return False
    return port_in_circuit_segments(segments, odf, port_local)


def link_segment_from_jump(payload: dict[str, Any], cid: str, display_name: str = "") -> dict[str, str] | None:
    """Store row for ODF-to-ODF jump leg (second segment of multi-hop)."""
    if (payload.get("local_type") or "").strip().lower() != "jump_odf":
        return None
    jump_odf = (payload.get("jump_odf") or "").strip()
    if not jump_odf:
        return None
    odf = (payload.get("odf") or "").strip()
    port_local = (payload.get("port_local") or "").strip()
    jump_fiber = (payload.get("jump_fiber") or port_local).strip()
    jump_cable = (payload.get("jump_cable") or "").strip()
    cable = (payload.get("cable") or "").strip()
    name = display_name or cid
    return {
        "cid": cid,
        "dev_a": odf,
        "port_a": port_local,
        "dev_b": jump_odf,
        "port_b": jump_fiber,
        "cab_a": cable,
        "cab_b": jump_cable or jump_odf,
        "cable": jump_cable or jump_odf,
        "name": name,
        "service": name,
        "ip": (payload.get("ip") or "").strip(),
        "loss_db": (payload.get("loss_db") or "").strip(),
        "label_pos": f"{odf} {port_local} -> {jump_odf} {jump_fiber} (跳纤段)",
    }


def trunk_segment_after_jump_landing(
    client: NetBoxClient,
    payload: dict[str, Any],
    cid: str,
    display_name: str = "",
) -> dict[str, str] | None:
    """After cross-cable jump, register the landing fiber's rear trunk to its peer ODF.

    Field flow: create on one trunk → continue on peer with 换缆跳纤 (e.g. 1-6→2-5).
    Without this row the FMS path stops at the landing front port and looks「路径不全」.
    Same-cable jumps do not add a new trunk (still on the incoming cable).
    """
    if (payload.get("local_type") or "").strip().lower() != "jump_odf":
        return None
    jump_odf = (payload.get("jump_odf") or "").strip()
    port_local = (payload.get("port_local") or "").strip()
    jump_fiber = (payload.get("jump_fiber") or port_local).strip()
    jump_cable = (payload.get("jump_cable") or "").strip()
    cable = (payload.get("cable") or "").strip()
    if not jump_odf or not jump_fiber:
        return None

    from naming_rules import cable_label_variants, normalize_fiber_label

    jump_fiber = normalize_fiber_label(jump_fiber)
    if jump_cable and cable:
        a_vars = set(cable_label_variants(cable))
        if jump_cable == cable or jump_cable in a_vars:
            return None

    if not jump_cable:
        # Prefer route cable on the jump target ODF; fall back to name-as-label.
        try:
            jump_cable = (
                cable_for_port_on_odf(
                    client,
                    jump_odf,
                    resolve_port_name(jump_fiber, jump_odf, device_name=jump_odf),
                )
                or ""
            )
        except Exception:
            jump_cable = ""
        if not jump_cable:
            jump_cable = jump_odf

    peer = resolve_peer_from_cable(client, jump_odf, jump_cable)
    if not peer:
        return None

    peer_cab = cable_label_on_odf(client, peer, jump_odf, jump_cable) or jump_cable
    name = display_name or cid
    seg = segment_from_field(
        jump_odf,
        peer,
        "a",
        jump_fiber,
        jump_fiber,
        cid=cid,
        cable=jump_cable,
        name=name,
        service=name,
        ip=(payload.get("ip") or "").strip(),
        loss_db=(payload.get("loss_db") or "").strip(),
    )
    seg["cab_a"] = jump_cable
    seg["cab_b"] = peer_cab
    seg["label_pos"] = f"{jump_odf} {jump_fiber} -> {peer} {jump_fiber}"
    return seg


def import_one_circuit(
    client: NetBoxClient,
    config: dict[str, Any],
    segments: list[dict[str, str]],
) -> dict[str, Any]:
    if not segments:
        raise ValueError("无光路段")

    segments = dedupe_segment_rows(segments)
    groups = group_by_circuit(segments)
    cid = segments[0]["cid"]
    segs = groups[cid]
    warnings = validate_chain(segs)

    for seg in segs:
        if not seg.get("cable"):
            seg["cable"] = infer_cable_between(client, seg["dev_a"], seg["dev_b"])

    links = sort_links_for_apply(
        merge_link_rows([], infer_links_from_segments(groups) + jump_links_from_segments(segs))
    )
    xf_splices = cross_fiber_splices_from_segments(segs)
    # Field portal cables already exist; skip provision/rename (10–20s). Only
    # apply jump/splice links that are not already written by apply_local_extension.
    if links or xf_splices:
        FiberInfra(client).apply_from_excel(
            segs,
            splices=xf_splices,
            links=links,
            link_mode="port",
            skip_cable_provision=True,
        )

    meta = merge_circuit_meta(segs)
    table_desc = build_table_description(segs, meta)

    desc_parts = [table_desc]
    if meta.get("loss_db"):
        wl = meta.get("wavelength") or "1310"
        desc_parts.append(f"全链路光损: {meta['loss_db']}dB@{wl}nm")

    circuit_payload = {
        "name": meta["name"],
        "cid": cid,
        "status": "active",
        "strand_count": 1,
        "description": "\n".join(desc_parts)[:2000],
        "comments": table_desc[:4000],
    }

    existing = client.request("GET", "/plugins/fms/fiber-circuits/", params={"cid": cid, "limit": 1})
    if existing.get("results"):
        circuit_id = existing["results"][0]["id"]
        client.request("PATCH", f"/plugins/fms/fiber-circuits/{circuit_id}/", json=circuit_payload)
    else:
        circuit_id = client.post("/plugins/fms/fiber-circuits/", circuit_payload)["id"]

    path_nodes, origin_id, dest_id = build_explicit_fms_path(client, segs)
    paths = client.request("GET", "/plugins/fms/fiber-circuit-paths/", params={"circuit_id": circuit_id, "limit": 10})
    # Trace/is_complete refine runs in background after HTTP return.
    provisional_complete = bool(origin_id and dest_id and origin_id != dest_id)
    path_payload = {
        "circuit": circuit_id,
        "position": 1,
        "origin": origin_id,
        "destination": dest_id,
        "path": path_nodes,
        "is_complete": provisional_complete,
        **_path_optical_fields(meta),
    }
    if paths.get("results"):
        path_id = _safe_path_id(paths["results"][0].get("id"))
        if not path_id:
            raise RuntimeError(f"光路 {cid} 在 NetBox 中的路径记录无效（缺少 path id）")
        client.request("PATCH", f"/plugins/fms/fiber-circuit-paths/{path_id}/", json=path_payload)
    else:
        created = client.post("/plugins/fms/fiber-circuit-paths/", path_payload)
        path_id = _safe_path_id(created.get("id") if isinstance(created, dict) else None)
        if not path_id:
            raise RuntimeError(f"创建 NetBox 路径失败：未返回有效 path id（光路 {cid}）")

    web_base = config.get("web_base_url", config["base_url"].replace("/api", ""))
    hops = len(path_nodes)
    occupancy_fp_ids = sorted(_fp_ids_from_path_nodes(path_nodes, origin_id, dest_id))
    trace_url = _trace_url_for_path(web_base, path_id)

    return {
        "cid": cid,
        "path_id": path_id,
        "circuit_id": circuit_id,
        "is_complete": provisional_complete,
        "hops": hops,
        "fiber_chain": "",
        "trace_url": trace_url,
        "route": build_route_short(segs),
        "warnings": warnings,
        "segment_count": len(segs),
        "occupancy_fp_ids": occupancy_fp_ids,
        "name": meta.get("name") or cid,
    }


def refresh_circuit_path(
    client: NetBoxClient,
    config: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild explicit FMS path from store after PATCH/LINK (retrace breaks cross-fiber/jump)."""
    cid = result.get("cid") or ""
    path_id = result.get("path_id")
    if not cid or not path_id:
        return result
    store = load_store()
    segs = store.get(cid, [])
    if not segs:
        return result
    path_nodes, origin_id, dest_id = build_explicit_fms_path(client, segs)
    web_base = config.get("web_base_url", config["base_url"].replace("/api", ""))
    client.request(
        "PATCH",
        f"/plugins/fms/fiber-circuit-paths/{path_id}/",
        json={
            "origin": origin_id,
            "destination": dest_id,
            "path": path_nodes,
            "is_complete": bool(origin_id and dest_id and origin_id != dest_id),
        },
    )
    trace = get_fms_path_trace(client, int(path_id), web_base, force_refresh=True)
    result["is_complete"] = _registered_endpoints_in_trace(trace, segs, client)
    client.request(
        "PATCH",
        f"/plugins/fms/fiber-circuit-paths/{path_id}/",
        json={"is_complete": bool(result["is_complete"])},
    )
    result["hops"] = len(path_nodes)
    chain, _ = build_fiber_chain_from_trace(trace)
    if chain:
        FiberInfra(client).apply_fiber_chain(int(path_id), trace=trace)
        result["fiber_chain"] = chain
    result["trace_url"] = _trace_url_for_path(web_base, path_id)
    return result


def _clean_device_name(value: Any) -> str:
    s = str(value or "").strip()
    if not s or s.lower() in ("undefined", "null", "none", "—", "-"):
        return ""
    return s


def submit_segment(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    client = client_from_config(config)
    odf = _clean_device_name(payload.get("odf"))
    cable = _clean_device_name(payload.get("cable"))
    peer = _clean_device_name(payload.get("peer_odf")) or resolve_peer_from_cable(client, odf, cable)
    mode = (payload.get("mode") or "continue").strip().lower()

    if mode == "new":
        business_name = (payload.get("business_name") or payload.get("name") or "").strip()
        cid = make_cid(business_name)
        if not cid:
            raise ValueError("请填写业务名称")
        display_name = business_name
    else:
        cid = (payload.get("cid") or "").strip()
        display_name = cid
        if not cid:
            raise ValueError("请选择已有光路")

    if not odf:
        raise ValueError("缺少 ODF框")
    if not peer:
        raise ValueError("无法从缆段解析对端 ODF，请检查 NetBox 缆线配置")

    port_local = (payload.get("port_local") or "").strip()
    port_peer = (payload.get("port_peer") or port_local).strip()

    store = load_store()
    existing_segs = store.get(cid, []) if mode == "continue" else []
    role = (payload.get("role") or resolve_segment_role(odf, port_local, existing_segs)).strip().lower()

    seg = segment_from_field(
        odf,
        peer,
        role,
        port_local,
        port_peer,
        cid=cid,
        cable=cable,
        name=display_name,
        service=display_name,
        ip=(payload.get("ip") or "").strip(),
        loss_db=(payload.get("loss_db") or "").strip(),
    )
    local_note = ""
    local_type = (payload.get("local_type") or "trunk_only").strip().lower()
    if local_type == "to_device":
        device = (payload.get("device") or "").strip()
        device_port = (payload.get("device_port") or "").strip()
        if device:
            local_note = f"本端直连 {device} {device_port}".strip()
            seg["label_pos"] = f"{seg['label_pos']} | {local_note}"
    elif local_type == "jump_odf":
        jump_odf = (payload.get("jump_odf") or "").strip()
        jump_fiber = (payload.get("jump_fiber") or port_local).strip()
        if jump_odf:
            local_note = f"本端跳纤 {jump_odf} {jump_fiber}"
            seg["label_pos"] = f"{seg['label_pos']} | {local_note}"

    if not seg["port_a"] or not seg["port_b"]:
        raise ValueError("请填写本端与对端纤芯")

    skip_segment = should_skip_segment_upsert(
        mode=mode,
        local_type=local_type,
        segments=existing_segs,
        odf=odf,
        port_local=port_local,
    )
    if not skip_segment and segment_is_reverse_of_existing(existing_segs, seg):
        skip_segment = True

    extra_occ: list[tuple[str, str, str]] = []
    if local_type == "jump_odf":
        jump_odf = (payload.get("jump_odf") or "").strip()
        jump_fiber = (payload.get("jump_fiber") or port_local).strip()
        jump_cable = (payload.get("jump_cable") or "").strip()
        extra_occ.append((jump_odf, jump_fiber, jump_cable))
        land_trunk = trunk_segment_after_jump_landing(client, payload, cid, display_name)
        if land_trunk:
            extra_occ.append(
                (
                    (land_trunk.get("dev_a") or "").strip(),
                    (land_trunk.get("port_a") or "").strip(),
                    (land_trunk.get("cab_a") or land_trunk.get("cable") or "").strip(),
                )
            )
            extra_occ.append(
                (
                    (land_trunk.get("dev_b") or "").strip(),
                    (land_trunk.get("port_b") or "").strip(),
                    (land_trunk.get("cab_b") or land_trunk.get("cable") or "").strip(),
                )
            )

    # Plan A: fresh occupancy check before writing (same cid allowed).
    # Jump target is always checked — continue/skip_segment used to skip it.
    if not skip_segment:
        assert_submit_ports_available(
            client,
            config,
            cid=cid,
            odf=odf,
            port_local=port_local,
            peer=peer,
            port_peer=port_peer,
            cable=cable,
            extra=extra_occ,
        )
    elif extra_occ:
        assert_submit_ports_available(
            client,
            config,
            cid=cid,
            odf="",
            port_local="",
            peer="",
            port_peer="",
            cable="",
            extra=extra_occ,
        )

    def _apply(store_data: dict[str, list[dict[str, str]]]) -> None:
        nonlocal skip_segment
        segs_now = store_data.get(cid, []) if mode == "continue" else []
        # Re-evaluate skip against latest store under lock
        if mode == "continue":
            skip_now = should_skip_segment_upsert(
                mode=mode,
                local_type=local_type,
                segments=segs_now,
                odf=odf,
                port_local=port_local,
            )
            if not skip_now and segment_is_reverse_of_existing(segs_now, seg):
                skip_now = True
            skip_segment = skip_now
        if not skip_segment:
            # Concurrent submit: port already claimed by another circuit in local store
            for other_cid, other_segs in store_data.items():
                if other_cid == cid:
                    continue
                for s in other_segs:
                    for dev, port in (
                        (s.get("dev_a", ""), s.get("port_a", "")),
                        (s.get("dev_b", ""), s.get("port_b", "")),
                    ):
                        if (dev == seg["dev_a"] and port == seg["port_a"]) or (
                            dev == seg["dev_b"] and port == seg["port_b"]
                        ):
                            raise ValueError(_conflict_message(dev, port, other_cid))
            upsert_segment(store_data, seg)
            store_data[cid] = dedupe_segment_rows(store_data.get(cid, []))
        elif cid not in store_data or not store_data[cid]:
            raise ValueError("该端口尚未登记缆段，请先登记缆段或从起点新建光路")

        # Jump/extension must always land in the store so import_one_circuit can
        # rebuild the full FMS path — previously only written when skip_segment.
        if local_type == "jump_odf":
            link_seg = link_segment_from_jump(payload, cid, display_name)
            if link_seg:
                for other_cid, other_segs in store_data.items():
                    if other_cid == cid:
                        continue
                    for s in other_segs:
                        for dev, port in (
                            (s.get("dev_a", ""), s.get("port_a", "")),
                            (s.get("dev_b", ""), s.get("port_b", "")),
                        ):
                            if (dev == link_seg["dev_b"] and port == link_seg["port_b"]) or (
                                dev == link_seg["dev_a"] and port == link_seg["port_a"]
                            ):
                                raise ValueError(_conflict_message(dev, port, other_cid))
                upsert_segment(store_data, link_seg)
                store_data[cid] = dedupe_segment_rows(store_data.get(cid, []))
            # Cross-cable landing: also register rear trunk so path does not stop at 跳纤口.
            land_trunk = trunk_segment_after_jump_landing(client, payload, cid, display_name)
            if land_trunk:
                upsert_segment(store_data, land_trunk)
                store_data[cid] = dedupe_segment_rows(store_data.get(cid, []))

    # Snapshot so a NetBox failure cannot leave the portal store ahead of FMS.
    store_before = load_store()
    segs_before = [dict(s) for s in store_before.get(cid, [])]

    mutate_store(_apply)
    store = load_store()

    payload["cid"] = cid
    created_cables: set[int] = set()
    try:
        before_ext = _submit_extension_cable_ids(client, payload)
        local_result = apply_local_extension(client, payload)
        after_ext = _submit_extension_cable_ids(client, payload)
        created_cables = after_ext - before_ext
        result = import_one_circuit(client, config, store[cid])
    except Exception:
        def _rollback(store_data: dict[str, list[dict[str, str]]]) -> None:
            if segs_before:
                store_data[cid] = [dict(s) for s in segs_before]
            else:
                store_data.pop(cid, None)

        try:
            mutate_store(_rollback)
        except Exception:
            pass
        try:
            _delete_dcim_cables_best_effort(client, created_cables)
        except Exception:
            pass
        try:
            if segs_before:
                import_one_circuit(client, config, segs_before)
            elif mode == "new":
                _delete_fms_paths_for_cid(client, cid)
                _delete_fms_circuit_by_cid(client, cid)
        except Exception:
            pass
        raise

    web_base = config.get("web_base_url", config["base_url"].replace("/api", ""))
    path_id = result.get("path_id")
    occupancy_fp_ids = {int(x) for x in (result.pop("occupancy_fp_ids", None) or []) if x}
    invalidate_after_circuit_change(int(path_id) if path_id else None, wipe_fp_index=False)
    if occupancy_fp_ids and path_id:
        try:
            upsert_fp_index_entries(
                web_base,
                occupancy_fp_ids,
                {
                    "cid": result.get("cid") or cid,
                    "name": result.get("name") or display_name or cid,
                    "ip": (payload.get("ip") or "").strip(),
                    "description": "",
                    "status": "active",
                    "is_complete": bool(result.get("is_complete")),
                    "hops_count": int(result.get("hops") or 0),
                    "path_id": int(path_id),
                    "circuit_id": result.get("circuit_id"),
                    "trace_url": result.get("trace_url") or "",
                    "fiber_chain": result.get("fiber_chain") or "",
                    "actual_loss_db": (payload.get("loss_db") or "").strip(),
                    "wavelength_nm": "",
                    "pending": False,
                },
            )
        except Exception:
            invalidate_fp_circuit_index_cache()
    if path_id:
        _finalize_path_in_background(
            config,
            int(path_id),
            [dict(s) for s in store.get(cid, [])],
            bool(result.get("is_complete")),
        )
    result["segment_skipped"] = skip_segment

    result["peer_odf"] = peer
    result["local_extension"] = local_result
    result["message"] = "光路段已登记" if not skip_segment else "本端延伸已登记"
    if local_result:
        if local_result["local_type"] == "jump_odf":
            result["message"] += f"，已创建跳纤 → {local_result['target']}"
        elif local_result["local_type"] == "to_device":
            result["message"] += f"，已直连 {local_result['device']}"
    if result["is_complete"]:
        result["message"] = "光路已完整，可追踪" + (f"（{local_note}）" if local_note else "")
    else:
        result["message"] = (
            f"已登记 {result['segment_count']} 段，路径尚未完整，请在下一 ODF 继续登记"
            + (f"（{local_note}）" if local_note else "")
        )
    return result


def submit_loss(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    client = client_from_config(config)
    odf = (payload.get("odf") or "").strip()
    port = (payload.get("port") or "").strip()
    loss = (payload.get("loss_db") or "").strip()
    cid = (payload.get("cid") or "").strip()
    cable = (payload.get("cable") or "").strip()
    if not odf or not port or not loss:
        raise ValueError("缺少 ODF框、纤芯或光损")

    err = None
    if not cable:
        store = load_store()
        segments = store.get(cid, []) if cid else []
        if not cid:
            for segs in store.values():
                segments.extend(segs)
        cable, err = resolve_cable_for_odf_loss(segments, odf, port, cid=cid, cable_hint="")
        if not cable:
            neighbors = neighbor_odfs(client, odf)
            if neighbors:
                cable = infer_cable_between(client, odf, neighbors[0]) or ""
    if not cable:
        raise ValueError(err or "无法解析缆段，请从现场端口进入后再保存光损")

    infra = FiberInfra(client)
    n = infra.apply_strand_losses(
        [
            {
                "odf": odf,
                "cable": cable,
                "port": port,
                "loss_db": loss,
                "wavelength": (payload.get("wavelength") or "1310").strip(),
                "cid": cid,
            }
        ]
    )
    try:
        from field_browser import invalidate_room_panel_cache, invalidate_strand_loss_cache

        invalidate_strand_loss_cache(cable)
        invalidate_room_panel_cache(
            site=(payload.get("site") or "").strip(),
            room=(payload.get("room") or "").strip(),
        )
    except Exception:
        pass
    return {"message": f"已写入 {n} 条光损", "odf": odf, "port": port, "cable": cable, "loss_db": loss}


def _segment_involves_port(seg: dict[str, str], odf: str, fiber: str, cable: str = "") -> bool:
    from naming_rules import normalize_fiber_label

    odf = (odf or "").strip()
    fiber = normalize_fiber_label((fiber or "").strip())
    cable = (cable or "").strip()
    variants = set(cable_label_variants(cable)) if cable else None
    seg_cable = (seg.get("cable") or "").strip()

    def _match(dev: str, port: str) -> bool:
        if (dev or "").strip() != odf:
            return False
        if normalize_fiber_label((port or "").strip()) != fiber:
            return False
        if variants is None:
            return True
        return not seg_cable or seg_cable in variants

    return _match(seg.get("dev_a", ""), seg.get("port_a", "")) or _match(seg.get("dev_b", ""), seg.get("port_b", ""))


def _delete_fms_circuit_by_cid(client: NetBoxClient, cid: str) -> bool:
    cid = (cid or "").strip()
    if not cid:
        return False
    hits = client.request("GET", "/plugins/fms/fiber-circuits/", params={"cid": cid, "limit": 1})
    if not hits.get("results"):
        return False
    client.request("DELETE", f"/plugins/fms/fiber-circuits/{hits['results'][0]['id']}/")
    return True


def _delete_fms_paths_for_cid(client: NetBoxClient, cid: str) -> list[int]:
    """Remove FMS paths so PATCH/LINK jump cables are no longer locked by trace nodes."""
    cid = (cid or "").strip()
    if not cid:
        return []
    hits = client.request("GET", "/plugins/fms/fiber-circuits/", params={"cid": cid, "limit": 1})
    if not hits.get("results"):
        return []
    circuit_id = hits["results"][0]["id"]
    paths = client.request("GET", "/plugins/fms/fiber-circuit-paths/", params={"circuit_id": circuit_id, "limit": 20})
    deleted: list[int] = []
    for path in paths.get("results", []):
        pid = path.get("id")
        if not pid:
            continue
        client.request("DELETE", f"/plugins/fms/fiber-circuit-paths/{pid}/")
        deleted.append(int(pid))
    return deleted


def clear_front_jumps_on_port(client: NetBoxClient, odf_name: str, fiber: str, cable: str) -> int:
    """Remove PATCH/LINK jump cables on one front port (keep trunk/rear attachment)."""
    if not odf_name or not fiber:
        return 0
    infra = FiberInfra(client)
    try:
        fp_name = resolve_port_name(fiber, cable or "", device_name=odf_name)
    except Exception:
        return 0
    try:
        return infra.clear_jump_cables_on_port(odf_name, fp_name)
    except RuntimeError:
        return 0


def _clear_jumps_on_segment_ports(
    client: NetBoxClient,
    segments: list[dict[str, str]],
    default_cable: str = "",
    seen: set[tuple[str, str]] | None = None,
) -> int:
    """Clear Front jumps on both ends of removed segment rows (multi-hop release)."""
    seen_ports = seen if seen is not None else set()
    removed = 0
    for seg in segments:
        seg_cable = (seg.get("cable") or default_cable or "").strip()
        for dev, port in ((seg.get("dev_a", ""), seg.get("port_a", "")), (seg.get("dev_b", ""), seg.get("port_b", ""))):
            dev = (dev or "").strip()
            port = (port or "").strip()
            if not dev or not port:
                continue
            key = (dev, port)
            if key in seen_ports:
                continue
            seen_ports.add(key)
            removed += clear_front_jumps_on_port(client, dev, port, seg_cable)
    return removed


def release_port_registration(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Remove port from circuit store, clear Front jumps, resync or delete FMS circuit."""
    from naming_rules import fiber_from_port_name, normalize_fiber_label

    client = client_from_config(config)
    odf = (payload.get("odf") or "").strip()
    cable = (payload.get("cable") or "").strip()
    raw_fiber = (payload.get("fiber") or payload.get("port_local") or "").strip()
    fiber = normalize_fiber_label(
        fiber_from_port_name(raw_fiber, cable) or fiber_from_port_name(raw_fiber, "") or raw_fiber
    )
    cid_hint = (payload.get("cid") or "").strip()
    scope = (payload.get("scope") or "port").strip().lower()
    if scope not in ("port", "jumps", "circuit"):
        scope = "port"
    if scope == "circuit":
        if not cid_hint:
            raise ValueError("删除整条光路请指定光路编号")
        # odf/fiber optional for full-circuit delete
    elif not odf or not fiber:
        raise ValueError("缺少 ODF框 或纤芯")

    if scope == "jumps":
        if not odf or not fiber:
            raise ValueError("缺少 ODF框 或纤芯")
        if cid_hint:
            _delete_fms_paths_for_cid(client, cid_hint)
        jumps_removed = clear_front_jumps_on_port(client, odf, fiber, cable)
        invalidate_after_circuit_change()
        return {
            "message": f"已删除本端口 {fiber} 的 {jumps_removed} 条跳纤/设备连线",
            "jumps_removed": jumps_removed,
            "scope": scope,
        }

    store = load_store()
    target_cids: list[str] = []
    if scope == "circuit":
        target_cids = [cid_hint]
    elif cid_hint and cid_hint in store:
        if any(_segment_involves_port(s, odf, fiber, cable) for s in store[cid_hint]):
            target_cids = [cid_hint]
        elif any(_segment_involves_port(s, odf, fiber, "") for s in store[cid_hint]):
            # Cable label mismatch (A/B 端命名) — still treat as hit.
            target_cids = [cid_hint]
    else:
        for cid_key, segs in store.items():
            if any(_segment_involves_port(seg, odf, fiber, cable) for seg in segs) or any(
                _segment_involves_port(seg, odf, fiber, "") for seg in segs
            ):
                target_cids.append(cid_key)

    if not target_cids and scope == "port":
        jumps_removed = 0
        # Port not found in store for this cid — only clear jumps; do NOT force-rebuild
        # the whole circuit (that used to delete FMS paths then fail on asymmetric rears).
        jumps_removed += clear_front_jumps_on_port(client, odf, fiber, cable)
        invalidate_after_circuit_change()
        if cid_hint and cid_hint in store and store[cid_hint]:
            ports = sorted(
                {
                    f"{s.get('dev_a')} {s.get('port_a')}"
                    for s in store[cid_hint]
                    if s.get("dev_a") and s.get("port_a")
                }
                | {
                    f"{s.get('dev_b')} {s.get('port_b')}"
                    for s in store[cid_hint]
                    if s.get("dev_b") and s.get("port_b")
                }
            )
            hint = "、".join(ports[:8]) + ("…" if len(ports) > 8 else "")
            raise ValueError(
                f"端口 {odf} {fiber} 不在光路 {cid_hint} 的登记段中"
                + (f"（登记端口：{hint}）" if hint else "")
                + "。可用「删除整条光路」，或选中登记过的纤芯再解除。"
                + (f" 已清除本端口 {jumps_removed} 条跳纤。" if jumps_removed else "")
            )
        if jumps_removed:
            return {
                "message": f"未找到登记段，已删除 {jumps_removed} 条跳纤",
                "jumps_removed": jumps_removed,
                "segments_removed": 0,
            }
        raise ValueError("未找到该端口的光路登记，可尝试「仅删除跳纤」或「删除整条光路」")

    jumps_removed = 0
    segments_removed = 0
    circuits_deleted: list[str] = []
    circuits_updated: list[str] = []
    sync_warnings: list[str] = []
    cleared_ports: set[tuple[str, str]] = set()

    for cid in target_cids:
        segs = list(store.get(cid, []))
        if scope == "circuit":
            removed_segs = segs
            kept: list[dict[str, str]] = []
            removed = len(segs)
        else:
            def _hit(s: dict[str, str]) -> bool:
                return _segment_involves_port(s, odf, fiber, cable) or _segment_involves_port(
                    s, odf, fiber, ""
                )

            removed_segs = [s for s in segs if _hit(s)]
            kept = [s for s in segs if not _hit(s)]
            removed = len(removed_segs)
        _delete_fms_paths_for_cid(client, cid)
        jumps_removed += _clear_jumps_on_segment_ports(client, removed_segs, cable, cleared_ports)
        segments_removed += removed
        if not kept:
            if _delete_fms_circuit_by_cid(client, cid):
                circuits_deleted.append(cid)
            store.pop(cid, None)
        else:
            store[cid] = dedupe_segment_rows(kept)
            try:
                import_one_circuit(client, config, store[cid])
                circuits_updated.append(cid)
            except Exception as exc:
                # Keep local store update even if NetBox rebuild fails (asymmetric ODF/rear).
                sync_warnings.append(f"{cid}: {exc}")

    if scope == "port":
        key = (odf, fiber)
        if key not in cleared_ports:
            jumps_removed += clear_front_jumps_on_port(client, odf, fiber, cable)

    save_store(store)
    invalidate_after_circuit_change()

    parts = []
    if segments_removed:
        parts.append(f"移除 {segments_removed} 段登记")
    if jumps_removed:
        parts.append(f"删除 {jumps_removed} 条跳纤")
    if circuits_deleted:
        parts.append(f"已删除光路 {', '.join(circuits_deleted)}")
    elif circuits_updated:
        parts.append(f"已更新光路 {', '.join(circuits_updated)}")
    msg = " · ".join(parts) if parts else "已完成"
    if sync_warnings:
        msg += "。本地登记已保存，但 NetBox 同步未完成：" + "；".join(sync_warnings)
    else:
        msg += "。可在新纤芯上重新登记或添加跳纤。"
    return {
        "message": msg,
        "segments_removed": segments_removed,
        "jumps_removed": jumps_removed,
        "circuits_deleted": circuits_deleted,
        "circuits_updated": circuits_updated,
        "sync_warnings": sync_warnings,
        "scope": scope,
    }


def _segment_port_conflict(
    segs: list[dict[str, str]],
    *,
    skip_index: int,
    dev_a: str,
    port_a: str,
    dev_b: str,
    port_b: str,
) -> str | None:
    for i, s in enumerate(segs):
        if i == skip_index:
            continue
        for dev, port in ((s.get("dev_a", ""), s.get("port_a", "")), (s.get("dev_b", ""), s.get("port_b", ""))):
            if dev == dev_a and port == port_a:
                return f"{dev_a} {port_a} 已被本光路其他段占用"
            if dev == dev_b and port == port_b:
                return f"{dev_b} {port_b} 已被本光路其他段占用"
    return None


def _fiber_candidates_after(current: str, fibers: list[str]) -> list[str]:
    ordered = sorted({f for f in fibers if f}, key=fiber_sort_key)
    if not ordered:
        return []
    if current not in ordered:
        return ordered
    idx = ordered.index(current)
    return ordered[idx + 1 :] + ordered[:idx]


def _resolve_fp_id(client: NetBoxClient, odf: str, port: str, cable: str) -> int | None:
    try:
        from naming_rules import normalize_fiber_label

        port = normalize_fiber_label(port)
        dev = find_device(client, odf)
        fp = find_front_port(
            client,
            dev["id"],
            resolve_port_name(port, cable, device_name=odf),
            port,
        )
        return int(fp["id"])
    except Exception:
        return None


def _port_taken_by_other_circuit(
    client: NetBoxClient,
    web_base: str,
    odf: str,
    port: str,
    cable: str,
    cid: str,
) -> bool:
    return bool(_other_circuit_on_port(client, web_base, odf, port, cable, cid))


def _simulated_order_after_jump_swap(
    ordered: list[dict[str, str]],
    seg_index: int,
    new_pa: str,
    new_pb: str,
) -> list[dict[str, str]]:
    sim = [dict(s) for s in ordered]
    old = sim[seg_index]
    old_pa = (old.get("port_a") or "").strip()
    old_pb = (old.get("port_b") or "").strip()
    da = (old.get("dev_a") or "").strip()
    db = (old.get("dev_b") or "").strip()
    jump = _is_jump_segment(old)
    if jump:
        old["label_pos"] = f"{da} {new_pa} -> {db} {new_pb} (跳纤段)"
    else:
        old["label_pos"] = f"{da} {new_pa} -> {db} {new_pb}"
    old["port_a"] = new_pa
    old["port_b"] = new_pb
    _apply_jump_swap_cascade(sim, seg_index, old_pa, old_pb, new_pa, new_pb)
    return sim


def _validate_jump_swap_plan(
    orig: list[dict[str, str]],
    sim: list[dict[str, str]],
    cid: str,
    client: NetBoxClient,
    web_base: str,
) -> str | None:
    def _ends(seg: dict[str, str]) -> tuple[tuple[str, str], tuple[str, str]]:
        return (
            ((seg.get("dev_a") or "").strip(), (seg.get("port_a") or "").strip()),
            ((seg.get("dev_b") or "").strip(), (seg.get("port_b") or "").strip()),
        )

    def _collect_ports(segs: list[dict[str, str]]) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for seg in segs:
            a, b = _ends(seg)
            if a[0] and a[1]:
                out.add(a)
            if b[0] and b[1]:
                out.add(b)
        return out

    def _port_cable(segs: list[dict[str, str]], dev: str, port: str) -> str:
        for seg in segs:
            da = (seg.get("dev_a") or "").strip()
            db = (seg.get("dev_b") or "").strip()
            pa = (seg.get("port_a") or "").strip()
            pb = (seg.get("port_b") or "").strip()
            cab_a = (seg.get("cab_a") or seg.get("cable") or "").strip()
            cab_b = (seg.get("cab_b") or seg.get("cable") or "").strip()
            if da == dev and pa == port:
                return cab_a
            if db == dev and pb == port:
                return cab_b
        return ""

    def _is_chain_joint(segs: list[dict[str, str]], key: tuple[str, str]) -> bool:
        """True when (dev,port) only bridges adjacent segments (B of i == A of i+1)."""
        joints = 0
        for i in range(len(segs) - 1):
            _, b = _ends(segs[i])
            a, _ = _ends(segs[i + 1])
            if b == key and a == key:
                joints += 1
        return joints == 1

    def _shared_by_adjacent(segs: list[dict[str, str]], key: tuple[str, str]) -> bool:
        """ODF meet point: same fiber on two neighboring store rows (any A/B role)."""
        idxs = [i for i, seg in enumerate(segs) if key in _ends(seg)]
        if len(idxs) != 2:
            return False
        return abs(idxs[0] - idxs[1]) == 1

    old_ports = _collect_ports(orig)
    new_ports = _collect_ports(sim)
    counts: Counter[tuple[str, str]] = Counter()
    for seg in sim:
        a, b = _ends(seg)
        if a[0] and a[1]:
            counts[a] += 1
        if b[0] and b[1]:
            counts[b] += 1
    for key, n in counts.items():
        if n <= 1:
            continue
        # Linear/hub path shares an ODF fiber across two adjacent segments — not a conflict.
        if n == 2 and (_is_chain_joint(sim, key) or _shared_by_adjacent(sim, key)):
            continue
        dev, port = key
        return f"{dev} {port} 在本光路中重复"

    added = new_ports - old_ports
    for dev, port in sorted(added, key=lambda x: (x[0], fiber_sort_key(x[1]))):
        cab = _port_cable(sim, dev, port)
        if _port_taken_by_other_circuit(client, web_base, dev, port, cab, cid):
            return f"{dev} {port} 已被其他光路占用"
    return None


def _apply_jump_swap_cascade(
    ordered: list[dict[str, str]],
    seg_index: int,
    old_pa: str,
    old_pb: str,
    new_pa: str,
    new_pb: str,
) -> None:
    old = ordered[seg_index]
    da = (old.get("dev_a") or "").strip()
    db = (old.get("dev_b") or "").strip()

    def _refresh_label(seg: dict[str, str]) -> None:
        sda = (seg.get("dev_a") or "").strip()
        sdb = (seg.get("dev_b") or "").strip()
        spa = (seg.get("port_a") or "").strip()
        spb = (seg.get("port_b") or "").strip()
        if _is_jump_segment(seg):
            seg["label_pos"] = f"{sda} {spa} -> {sdb} {spb} (跳纤段)"
        else:
            seg["label_pos"] = f"{sda} {spa} -> {sdb} {spb}"

    # Scan every other row (do not break on a gap). An unrelated middle segment
    # used to stop the walk and leave later jumps on the old fiber.
    if old_pb != new_pb:
        for j in range(len(ordered)):
            if j == seg_index:
                continue
            seg = ordered[j]
            touched = False
            if (seg.get("dev_a") or "").strip() == db and (seg.get("port_a") or "").strip() == old_pb:
                ordered[j]["port_a"] = new_pb
                touched = True
                if (seg.get("port_b") or "").strip() == old_pb and not _is_jump_segment(seg):
                    ordered[j]["port_b"] = new_pb
            if (seg.get("dev_b") or "").strip() == db and (seg.get("port_b") or "").strip() == old_pb:
                ordered[j]["port_b"] = new_pb
                touched = True
                if (seg.get("port_a") or "").strip() == old_pb and not _is_jump_segment(seg):
                    ordered[j]["port_a"] = new_pb
            if touched:
                _refresh_label(ordered[j])
    if old_pa != new_pa:
        for j in range(len(ordered)):
            if j == seg_index:
                continue
            seg = ordered[j]
            touched = False
            if (seg.get("dev_b") or "").strip() == da and (seg.get("port_b") or "").strip() == old_pa:
                ordered[j]["port_b"] = new_pa
                touched = True
                if (seg.get("port_a") or "").strip() == old_pa and not _is_jump_segment(seg):
                    ordered[j]["port_a"] = new_pa
            if (seg.get("dev_a") or "").strip() == da and (seg.get("port_a") or "").strip() == old_pa:
                ordered[j]["port_a"] = new_pa
                touched = True
                if (seg.get("port_b") or "").strip() == old_pa and not _is_jump_segment(seg):
                    ordered[j]["port_b"] = new_pa
            if touched:
                _refresh_label(ordered[j])


def suggest_auto_jump_swap_ports(
    client: NetBoxClient,
    config: dict[str, Any],
    ordered: list[dict[str, str]],
    seg_index: int,
    cid: str,
) -> tuple[str, str]:
    """Pick next free fiber(s) for a jump segment (换缆跳纤 rotates landing port)."""
    seg = ordered[seg_index]
    if not _is_jump_segment(seg):
        raise ValueError("仅跳纤段支持自动换芯")

    web_base = config.get("web_base_url", config["base_url"].replace("/api", ""))
    da = (seg.get("dev_a") or "").strip()
    db = (seg.get("dev_b") or "").strip()
    pa = (seg.get("port_a") or "").strip()
    pb = (seg.get("port_b") or "").strip()
    cab_a = (seg.get("cab_a") or seg.get("cable") or "").strip()
    cab_b = (seg.get("cab_b") or seg.get("cable") or "").strip()
    same_odf = da == db
    cross_cable = bool(cab_a and cab_b and cab_a != cab_b)
    cross_fiber = bool(pa and pb and pa != pb)

    def _try(new_a: str, new_b: str) -> tuple[str, str] | None:
        if not new_a or not new_b:
            return None
        if new_a == pa and new_b == pb:
            return None
        sim = _simulated_order_after_jump_swap(ordered, seg_index, new_a, new_b)
        err = _validate_jump_swap_plan(ordered, sim, cid, client, web_base)
        return (new_a, new_b) if not err else None

    # Same-ODF / cross-ODF 换缆异纤：固定本端，旋转对端落地纤芯。
    if (same_odf and cross_cable) or (not same_odf and (cross_cable or cross_fiber)):
        for cand in _fiber_candidates_after(pb, list_odf_fibers(client, db, cab_b)):
            hit = _try(pa, cand)
            if hit:
                return hit

    if not same_odf:
        fibers_a = set(list_odf_fibers(client, da, cab_a))
        fibers_b = set(list_odf_fibers(client, db, cab_b))
        common = sorted(fibers_a & fibers_b, key=fiber_sort_key)
        for cand in _fiber_candidates_after(pa, common):
            hit = _try(cand, cand)
            if hit:
                return hit
        # Last resort: rotate A on its cable, keep B (when landing is already fixed).
        for cand in _fiber_candidates_after(pa, list_odf_fibers(client, da, cab_a)):
            hit = _try(cand, pb)
            if hit:
                return hit
    else:
        fibers = list_odf_fibers(client, da, cab_a or cab_b)
        for cand in _fiber_candidates_after(pa, fibers):
            hit = _try(cand, cand)
            if hit:
                return hit

    raise ValueError("没有可用的空闲纤芯可自动换芯")

def swap_segment_fiber(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Replace one registered segment's fibers (trunk or jump) and rebuild FMS path."""
    client = client_from_config(config)
    cid = (payload.get("cid") or "").strip()
    if not cid:
        raise ValueError("缺少光路编号")
    try:
        seg_index = int(payload.get("segment_index", -1))
    except (TypeError, ValueError):
        seg_index = -1
    auto = bool(payload.get("auto"))
    dry_run = bool(payload.get("dry_run"))
    new_pa = (payload.get("port_a") or payload.get("new_port_a") or "").strip()
    new_pb = (payload.get("port_b") or payload.get("new_port_b") or new_pa).strip()

    store = load_store()
    web_base = config.get("web_base_url", config["base_url"].replace("/api", ""))
    path_id = payload.get("path_id")
    try:
        path_id = int(path_id) if path_id else None
    except (TypeError, ValueError):
        path_id = None
    if cid not in store or not store[cid]:
        ensure_circuit_segments_in_store(
            config,
            client,
            cid,
            path_id=path_id,
            display_name=cid,
        )
        store = load_store()
    # Do NOT replace an existing store with FMS-inferred rows here.
    # Inferred traces from a truncated path would drop later trunks/jumps and
    # then rebuild an incomplete NetBox path after 跳纤换芯.

    ordered = _ordered_store_segments(store[cid])
    if seg_index < 0 or seg_index >= len(ordered):
        raise ValueError("无效的段序号")

    old = dict(ordered[seg_index])
    if auto and _is_jump_segment(old):
        if not new_pa or not new_pb:
            new_pa, new_pb = suggest_auto_jump_swap_ports(client, config, ordered, seg_index, cid)
        if dry_run:
            cross = "" if new_pa == new_pb else f"（异纤 {new_pa}→{new_pb}）"
            return {
                "dry_run": True,
                "auto": True,
                "port_a": new_pa,
                "port_b": new_pb,
                "segment_index": seg_index,
                "cid": cid,
                "message": (
                    f"自动跳纤换芯：{old.get('port_a')}↔{old.get('port_b')} → {new_pa}↔{new_pb}{cross}"
                ),
            }
    elif not new_pa or not new_pb:
        raise ValueError("请填写新纤芯")

    if old.get("port_a") == new_pa and old.get("port_b") == new_pb:
        raise ValueError("新纤芯与当前相同")

    sim = _simulated_order_after_jump_swap(ordered, seg_index, new_pa, new_pb)
    plan_err = _validate_jump_swap_plan(ordered, sim, cid, client, web_base)
    if plan_err:
        raise ValueError(plan_err)

    _delete_fms_paths_for_cid(client, cid)
    jumps_removed = _clear_jumps_on_segment_ports(client, [old])

    ordered_before = [dict(s) for s in ordered]
    old_pa = (old.get("port_a") or "").strip()
    old_pb = (old.get("port_b") or "").strip()
    new_seg = dict(old)
    new_seg["port_a"] = new_pa
    new_seg["port_b"] = new_pb
    if _is_jump_segment(old):
        new_seg["label_pos"] = f"{old.get('dev_a', '')} {new_pa} -> {old.get('dev_b', '')} {new_pb} (跳纤段)"
    else:
        new_seg["label_pos"] = f"{old.get('dev_a', '')} {new_pa} -> {old.get('dev_b', '')} {new_pb}"
    ordered[seg_index] = new_seg
    # Trunk 异纤换芯也要级联：对端 1-6→1-7 时，后续跳纤起点必须跟着改，
    # 否则跳纤段仍挂在旧纤芯上，路径会变成「1-6→1-7 无跳纤、轧钢段悬空」。
    _apply_jump_swap_cascade(ordered, seg_index, old_pa, old_pb, new_pa, new_pb)
    store[cid] = dedupe_segment_rows(ordered)
    save_store(store)

    try:
        result = import_one_circuit(client, config, store[cid])
    except Exception as exc:
        store[cid] = dedupe_segment_rows(ordered_before)
        save_store(store)
        raise ValueError(f"换芯失败，已回滚登记: {exc}") from exc

    web_base = config.get("web_base_url", config["base_url"].replace("/api", ""))
    path_id = result.get("path_id")
    invalidate_after_circuit_change(int(path_id) if path_id else None)
    if path_id:
        try:
            warm_trace_cache(client, int(path_id), web_base)
        except Exception:
            pass

    seg_type = "跳纤" if _is_jump_segment(old) else "缆段"
    cross = "" if new_pa == new_pb else f"（异纤 {new_pa}→{new_pb}）"
    return {
        **result,
        "message": (
            f"已换芯：{seg_type} {old.get('port_a')}↔{old.get('port_b')} → {new_pa}↔{new_pb}{cross}，"
            f"删除 {jumps_removed} 条旧跳纤，路径已重建"
        ),
        "old_port_a": old.get("port_a"),
        "old_port_b": old.get("port_b"),
        "port_a": new_pa,
        "port_b": new_pb,
        "segment_index": seg_index,
    }


def rename_circuit_business_name(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Rename FiberCircuit.name only; keep CID unchanged. Sync local store name/service."""
    cid = (payload.get("cid") or "").strip()
    new_name = (payload.get("business_name") or payload.get("name") or "").strip()
    if not cid:
        raise ValueError("缺少光路编号 cid")
    if not new_name:
        raise ValueError("请填写新的业务名称")
    if new_name.upper().startswith("FIB-") and new_name != cid:
        raise ValueError("业务名称不要以 FIB- 开头（那是光路编号）；请只填业务名，例如「轧钢监控」")

    client = client_from_config(config)
    hits = client.request("GET", "/plugins/fms/fiber-circuits/", params={"cid": cid, "limit": 5})
    results = hits.get("results") or []
    if not results:
        # fallback: match by name==cid for older rows
        all_c = paginate(client, "/plugins/fms/fiber-circuits/")
        results = [c for c in all_c if (c.get("cid") or "").strip() == cid]
    if not results:
        raise ValueError(f"NetBox 中找不到光路 {cid}")

    circ = results[0]
    circuit_id = int(circ["id"])
    old_name = (circ.get("name") or "").strip()
    if old_name == new_name:
        return {
            "cid": cid,
            "name": new_name,
            "old_name": old_name,
            "circuit_id": circuit_id,
            "message": f"业务名称未变化：{new_name}",
            "changed": False,
        }

    client.request("PATCH", f"/plugins/fms/fiber-circuits/{circuit_id}/", json={"name": new_name})

    store = load_store()
    store_updated = 0
    if cid in store:
        for seg in store[cid]:
            if (seg.get("name") or "") != new_name or (seg.get("service") or "") != new_name:
                seg["name"] = new_name
                seg["service"] = new_name
                store_updated += 1
        if store_updated:
            save_store(store)

    invalidate_after_circuit_change()
    return {
        "cid": cid,
        "name": new_name,
        "old_name": old_name,
        "circuit_id": circuit_id,
        "store_segments_updated": store_updated,
        "changed": True,
        "message": f"已改名：{old_name or cid} → {new_name}（编号 {cid} 不变）",
    }


def finalize_circuit(config: dict[str, Any], cid: str) -> dict[str, Any]:
    store = load_store()
    if cid not in store or not store[cid]:
        raise ValueError(f"光路 {cid} 无已登记段")
    client = client_from_config(config)
    result = import_one_circuit(client, config, store[cid])
    web_base = config.get("web_base_url", config["base_url"].replace("/api", ""))
    path_id = result.get("path_id")
    invalidate_after_circuit_change(int(path_id) if path_id else None)
    if path_id:
        try:
            warm_trace_cache(client, int(path_id), web_base)
        except Exception:
            pass
    return result
