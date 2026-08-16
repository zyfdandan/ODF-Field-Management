"""Server-side cache for FMS trace, fp index, room panels, and tree snapshots."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

# Layered TTL: memory shorter, disk longer. Traces stay until submit/invalidate.
FP_INDEX_MEM_TTL_SEC = 600
FP_INDEX_DISK_TTL_SEC = 3600
TRACE_MEM_TTL_SEC = 24 * 3600
TRACE_CACHE_TTL_SEC = TRACE_MEM_TTL_SEC  # backward-compatible alias
TRACE_DISK_TTL_SEC = 30 * 24 * 3600
ROOM_PANEL_MEM_TTL_SEC = 600
ROOM_PANEL_DISK_TTL_SEC = 30 * 24 * 3600  # keep full room snapshots; background warmup refreshes
TREE_MEM_TTL_SEC = 600
TREE_DISK_TTL_SEC = 7 * 24 * 3600
WARMUP_INTERVAL_SEC = 3600  # 后台自动刷新占用缓存：1 小时

TRACE_CACHE_PATH = Path(__file__).with_name("trace_cache.json")
FP_INDEX_CACHE_PATH = Path(__file__).with_name("fp_circuit_index.json")
ROOM_PANEL_CACHE_PATH = Path(__file__).with_name("room_panel_cache.json")
TREE_CACHE_PATH = Path(__file__).with_name("tree_cache.json")

_lock = threading.Lock()
_trace_mem: dict[str, dict[str, Any]] = {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _web_base_key(web_base: str) -> str:
    return (web_base or "").rstrip("/")


def _room_key(site: str, room: str) -> str:
    return f"{site}|{room}"


# --- trace cache ---


def invalidate_trace_cache(*path_ids: int) -> None:
    """Drop cached trace for path_id(s). No args clears all trace entries."""
    with _lock:
        if not path_ids:
            _trace_mem.clear()
            _write_json(TRACE_CACHE_PATH, {"entries": {}})
            return
        disk = _read_json(TRACE_CACHE_PATH)
        entries = dict(disk.get("entries") or {})
        for pid in path_ids:
            key = str(int(pid))
            entries.pop(key, None)
            _trace_mem.pop(key, None)
        _write_json(TRACE_CACHE_PATH, {"entries": entries})


def get_fms_path_trace(
    client: Any,
    path_id: int,
    web_base: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return FMS trace JSON, using memory/disk cache unless force_refresh."""
    web_base = _web_base_key(web_base)
    key = str(int(path_id))
    now = time.time()

    if not force_refresh:
        with _lock:
            mem = _trace_mem.get(key)
            if mem and mem.get("web_base") == web_base and now - float(mem.get("ts", 0)) < TRACE_MEM_TTL_SEC:
                return mem["trace"]
        entry = (_read_json(TRACE_CACHE_PATH).get("entries") or {}).get(key)
        if entry and entry.get("web_base") == web_base:
            age = now - float(entry.get("ts", 0) or 0)
            if age < TRACE_DISK_TTL_SEC:
                with _lock:
                    _trace_mem[key] = entry
                return entry["trace"]

    trace = client.request("GET", f"/plugins/fms/fiber-circuit-paths/{path_id}/trace/")
    stored = {"ts": time.time(), "web_base": web_base, "trace": trace}
    with _lock:
        _trace_mem[key] = stored
        disk = _read_json(TRACE_CACHE_PATH)
        entries = dict(disk.get("entries") or {})
        entries[key] = stored
        _write_json(TRACE_CACHE_PATH, {"entries": entries})
    return trace


def warm_trace_cache(client: Any, path_id: int, web_base: str, *, force_refresh: bool = False) -> None:
    if path_id:
        get_fms_path_trace(client, int(path_id), web_base, force_refresh=force_refresh)


def collect_path_ids_from_index(index: dict[int, list[dict[str, Any]]]) -> set[int]:
    out: set[int] = set()
    for circuits in index.values():
        for c in circuits:
            pid = c.get("path_id")
            if pid and not c.get("pending"):
                try:
                    out.add(int(pid))
                except (TypeError, ValueError):
                    continue
    return out


# --- fp circuit index disk ---


def invalidate_fp_index_disk() -> None:
    with _lock:
        if FP_INDEX_CACHE_PATH.exists():
            FP_INDEX_CACHE_PATH.unlink(missing_ok=True)


def load_fp_index_from_disk(web_base: str) -> dict[int, list[dict[str, Any]]] | None:
    web_base = _web_base_key(web_base)
    blob = _read_json(FP_INDEX_CACHE_PATH)
    if not blob or blob.get("web_base") != web_base:
        return None
    if time.time() - float(blob.get("ts", 0)) >= FP_INDEX_DISK_TTL_SEC:
        return None
    raw = blob.get("data") or {}
    out: dict[int, list[dict[str, Any]]] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = list(v)
        except (TypeError, ValueError):
            continue
    return out


def save_fp_index_to_disk(web_base: str, index: dict[int, list[dict[str, Any]]]) -> None:
    web_base = _web_base_key(web_base)
    payload = {
        "ts": time.time(),
        "web_base": web_base,
        "data": {str(k): v for k, v in index.items()},
    }
    with _lock:
        _write_json(FP_INDEX_CACHE_PATH, payload)


# --- room panel disk ---


def invalidate_room_panel_disk(*, site: str = "", room: str = "") -> None:
    with _lock:
        if not ROOM_PANEL_CACHE_PATH.exists():
            return
        if not site:
            ROOM_PANEL_CACHE_PATH.unlink(missing_ok=True)
            return
        disk = _read_json(ROOM_PANEL_CACHE_PATH)
        entries = dict(disk.get("entries") or {})
        if room:
            entries.pop(_room_key(site, room), None)
        else:
            prefix = f"{site}|"
            for k in list(entries.keys()):
                if k.startswith(prefix):
                    entries.pop(k, None)
        _write_json(ROOM_PANEL_CACHE_PATH, {"entries": entries})


def load_room_panel_from_disk(site: str, room: str) -> dict[str, Any] | None:
    data, _ts = load_room_panel_entry(site, room)
    return data


def load_room_panel_entry(
    site: str, room: str, *, allow_stale: bool = False
) -> tuple[dict[str, Any] | None, float | None]:
    key = _room_key(site, room)
    entry = (_read_json(ROOM_PANEL_CACHE_PATH).get("entries") or {}).get(key)
    if not entry:
        return None, None
    ts = float(entry.get("ts", 0) or 0)
    if time.time() - ts >= ROOM_PANEL_DISK_TTL_SEC and not allow_stale:
        return None, None
    data = entry.get("data")
    return (dict(data) if isinstance(data, dict) else None), ts


def save_room_panel_to_disk(site: str, room: str, data: dict[str, Any]) -> None:
    key = _room_key(site, room)
    with _lock:
        disk = _read_json(ROOM_PANEL_CACHE_PATH)
        entries = dict(disk.get("entries") or {})
        entries[key] = {"ts": time.time(), "data": data}
        _write_json(ROOM_PANEL_CACHE_PATH, {"entries": entries})


def list_warmed_room_keys() -> list[str]:
    """All room-panel disk keys, including stale (full-cache warmup / occupancy refresh)."""
    entries = _read_json(ROOM_PANEL_CACHE_PATH).get("entries") or {}
    return [k for k in entries.keys() if k and "|" in str(k)]


# --- tree disk ---


def invalidate_tree_disk() -> None:
    with _lock:
        if TREE_CACHE_PATH.exists():
            TREE_CACHE_PATH.unlink(missing_ok=True)


def load_tree_from_disk() -> dict[str, Any] | None:
    blob = _read_json(TREE_CACHE_PATH)
    if not blob:
        return None
    if time.time() - float(blob.get("ts", 0)) >= TREE_DISK_TTL_SEC:
        return None
    data = blob.get("data")
    return dict(data) if isinstance(data, dict) else None


def save_tree_to_disk(data: dict[str, Any]) -> None:
    with _lock:
        _write_json(TREE_CACHE_PATH, {"ts": time.time(), "data": data})


# --- bulk invalidation (webhook / admin) ---


def invalidate_all_caches() -> None:
    invalidate_trace_cache()
    invalidate_fp_index_disk()
    invalidate_room_panel_disk()
    invalidate_tree_disk()


def cache_status() -> dict[str, Any]:
    trace_entries = _read_json(TRACE_CACHE_PATH).get("entries") or {}
    room_entries = _read_json(ROOM_PANEL_CACHE_PATH).get("entries") or {}
    fp_blob = _read_json(FP_INDEX_CACHE_PATH)
    tree_blob = _read_json(TREE_CACHE_PATH)
    now = time.time()
    return {
        "ttl_sec": {
            "fp_index_mem": FP_INDEX_MEM_TTL_SEC,
            "fp_index_disk": FP_INDEX_DISK_TTL_SEC,
            "trace_mem": TRACE_MEM_TTL_SEC,
            "trace_disk": TRACE_DISK_TTL_SEC,
            "room_panel_mem": ROOM_PANEL_MEM_TTL_SEC,
            "room_panel_disk": ROOM_PANEL_DISK_TTL_SEC,
            "tree_mem": TREE_MEM_TTL_SEC,
            "tree_disk": TREE_DISK_TTL_SEC,
            "warmup_interval": WARMUP_INTERVAL_SEC,
        },
        "trace_count": len(trace_entries),
        "room_panel_count": len(room_entries),
        "fp_index_age_sec": round(now - float(fp_blob.get("ts", 0)), 1) if fp_blob else None,
        "tree_age_sec": round(now - float(tree_blob.get("ts", 0)), 1) if tree_blob else None,
        "trace_mem_count": len(_trace_mem),
    }
