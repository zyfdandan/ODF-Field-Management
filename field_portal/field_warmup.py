#!/usr/bin/env python3
"""Background cache warmup for Field API.

Startup (non-blocking): complete fp-index once, prime disk panels, then build any
tree rooms that have no snapshot. Periodic loop refreshes the index and merges
occupancy into cached panels (structure rebuild only if FIELD_WARMUP_ROOMS=1).
Manual refresh: POST /cache/refresh (occupancy default, or mode=full).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from field_cache import WARMUP_INTERVAL_SEC, collect_path_ids_from_index, list_warmed_room_keys, warm_trace_cache
from field_service import (
    build_fp_circuit_index,
    client_from_config,
    load_config,
    touch_fp_index_cache,
)

_PROGRESS_LOCK = threading.Lock()
_ACTIVE_REQ_LOCK = threading.Lock()
_ACTIVE_REQS = 0
_LAST_REQ_TS = 0.0
_SKIP_GATE_PATHS = {
    "/health",
    "/cache/status",
    "/api/cache/status",
    "/cache/refresh",
    "/api/cache/refresh",
    "/cache/invalidate",
    "/api/cache/invalidate",
}
_PROGRESS: dict[str, Any] = {
    "running": False,
    "mode": "",
    "phase": "idle",
    "label": "",
    "current": "",
    "i": 0,
    "n": 0,
    "pct": 0,
    "started_ts": 0.0,
    "finished_ts": 0.0,
    "error": "",
    "summary": None,
}


def _web_base(cfg: dict[str, Any]) -> str:
    return cfg.get("web_base_url", cfg["base_url"].replace("/api", "")).rstrip("/")


def _log(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


def mark_request_start() -> None:
    global _ACTIVE_REQS, _LAST_REQ_TS
    with _ACTIVE_REQ_LOCK:
        _ACTIVE_REQS += 1
        _LAST_REQ_TS = time.time()


def mark_request_end() -> None:
    global _ACTIVE_REQS, _LAST_REQ_TS
    with _ACTIVE_REQ_LOCK:
        _ACTIVE_REQS = max(0, _ACTIVE_REQS - 1)
        _LAST_REQ_TS = time.time()


@contextmanager
def request_gate(path: str) -> Iterator[None]:
    """Pause background warmup while a field user request is in flight."""
    p = (path or "").split("?", 1)[0].rstrip("/") or "/"
    skip = p in _SKIP_GATE_PATHS
    if skip:
        yield
        return
    mark_request_start()
    try:
        yield
    finally:
        mark_request_end()


def yield_to_interactive(*, idle_sec: float = 0.8) -> None:
    """Warmup waits until portal requests have been idle for idle_sec."""
    waited = False
    while True:
        with _ACTIVE_REQ_LOCK:
            n = _ACTIVE_REQS
            last = _LAST_REQ_TS
        if n <= 0 and (time.time() - last) >= idle_sec:
            if waited:
                _log("[warmup] resumed after interactive request")
            return
        if not waited:
            _log("[warmup] paused for interactive request")
            waited = True
        time.sleep(0.2)


def warmup_progress() -> dict[str, Any]:
    with _PROGRESS_LOCK:
        snap = dict(_PROGRESS)
    started = float(snap.get("started_ts") or 0)
    finished = float(snap.get("finished_ts") or 0)
    now = time.time()
    if started:
        snap["elapsed_sec"] = round((finished or now) - started, 1)
    else:
        snap["elapsed_sec"] = 0
    n = int(snap.get("n") or 0)
    i = int(snap.get("i") or 0)
    phase = snap.get("phase") or "idle"
    if phase in ("done", "error", "idle") and not snap.get("running"):
        pct = 100 if phase == "done" else 0
    elif n > 0:
        pct = min(99, int(100 * i / n))
    elif phase in ("fp_index", "tree", "queued"):
        pct = 3
    else:
        pct = int(snap.get("pct") or 0)
    snap["pct"] = pct
    return snap


def _set_progress(**kwargs: Any) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.update(kwargs)


def _try_begin(mode: str, label: str) -> bool:
    with _PROGRESS_LOCK:
        if _PROGRESS.get("running"):
            return False
        _PROGRESS.update(
            running=True,
            mode=mode,
            phase="queued",
            label=label,
            current="",
            i=0,
            n=0,
            pct=0,
            started_ts=time.time(),
            finished_ts=0.0,
            error="",
            summary=None,
        )
        return True


def _finish(summary: dict[str, Any] | None = None, error: str = "") -> None:
    phase = "error" if error else "done"
    label = error or "缓存已更新"
    _set_progress(
        running=False,
        phase=phase,
        label=label,
        finished_ts=time.time(),
        error=error,
        summary=summary,
        pct=100 if not error else _PROGRESS.get("pct") or 0,
    )


def _progress_room(i: int, n: int, site: str, room: str, *, phase: str = "rooms") -> None:
    current = f"{site}/{room}"
    _set_progress(
        phase=phase,
        i=i,
        n=n,
        current=current,
        label=f"{i}/{n} {current}",
    )


def _normalize_mode(mode: str) -> str:
    mode = (mode or "occupancy").strip().lower()
    if mode in ("occupancy", "default", "occ", ""):
        return "occupancy"
    if mode in ("full", "rebuild", "all", "structure"):
        return "full"
    raise ValueError("mode 只能是 occupancy 或 full")


def warm_room_panels_from_disk(cfg: dict[str, Any] | None = None) -> int:
    """Load previously visited room panels from disk into memory (no NetBox round-trip)."""
    from field_browser import prime_room_panels_from_disk

    _ = cfg
    ok = prime_room_panels_from_disk()
    if ok:
        _log(f"[warmup] room panels primed from disk: {ok}")
    return ok


def warm_all_room_panels(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prime every tree room so the first UI open is a cache hit."""
    from field_browser import (
        get_location_tree,
        get_room_port_panel,
        list_tree_rooms,
        prime_room_panels_from_disk,
        room_panel_on_disk,
    )

    cfg = cfg or load_config()
    client = client_from_config(cfg)
    web_base = _web_base(cfg)

    primed = prime_room_panels_from_disk()
    tree = get_location_tree(client, force_refresh=False)
    rooms = list_tree_rooms(tree)
    n = len(rooms)
    built = 0
    stamped = 0
    failed = 0
    _log(f"[warmup] full cache: {n} rooms (disk primed {primed})")

    for i, (site, room) in enumerate(rooms, 1):
        yield_to_interactive()
        _progress_room(i, n, site, room)
        _log(f"[warmup] room {i}/{n} {site}/{room}")
        try:
            if room_panel_on_disk(site, room):
                get_room_port_panel(
                    client, site, room, web_base, occupancy_only=True, force_refresh=False
                )
                stamped += 1
            else:
                get_room_port_panel(client, site, room, web_base, force_refresh=False)
                built += 1
            touch_fp_index_cache()
        except Exception as exc:
            failed += 1
            _log(f"[warmup] room {site}/{room} failed: {exc}")

    summary = {
        "rooms": n,
        "primed": primed,
        "built": built,
        "occupancy_stamped": stamped,
        "failed": failed,
    }
    _log(f"[warmup] full cache done: {summary}")
    try:
        yield_to_interactive(idle_sec=1.5)
        traces = warm_all_traces(cfg)
        summary["traces_warmed"] = traces
    except Exception as exc:
        _log(f"[warmup] traces failed: {exc}")
    return summary


def warm_all_traces(cfg: dict[str, Any] | None = None) -> int:
    """Prime FMS path traces from disk (fetch NetBox only when missing)."""
    cfg = cfg or load_config()
    client = client_from_config(cfg)
    web_base = _web_base(cfg)
    index = build_fp_circuit_index(client, web_base, force_refresh=False)
    path_ids = collect_path_ids_from_index(index)
    n = len(path_ids)
    ok = 0
    _log(f"[warmup] traces: {n} paths")
    for i, pid in enumerate(sorted(path_ids), 1):
        yield_to_interactive()
        _set_progress(phase="traces", i=i, n=n, current=str(pid), label=f"链路追踪 {i}/{n}")
        try:
            warm_trace_cache(client, pid, web_base, force_refresh=False)
            ok += 1
        except Exception as exc:
            _log(f"[warmup] trace path_id={pid} failed: {exc}")
    _log(f"[warmup] traces done: {ok}/{n}")
    return ok


def refresh_cached_room_occupancy(cfg: dict[str, Any] | None = None) -> int:
    """Merge current fp-index occupancy into every cached room (no structure rebuild)."""
    from field_browser import get_location_tree, get_room_port_panel, list_tree_rooms

    cfg = cfg or load_config()
    client = client_from_config(cfg)
    web_base = _web_base(cfg)
    rooms = list_tree_rooms(get_location_tree(client, force_refresh=False))
    if not rooms:
        keys = list_warmed_room_keys()
        rooms = [tuple(k.split("|", 1)) for k in keys if "|" in k]
    ok = 0
    n = len(rooms)
    for i, (site, room) in enumerate(rooms, 1):
        yield_to_interactive()
        _progress_room(i, n, site, room)
        _log(f"[warmup] occupancy {i}/{n} {site}/{room}")
        try:
            get_room_port_panel(
                client, site, room, web_base, occupancy_only=True, force_refresh=False
            )
            touch_fp_index_cache()
            ok += 1
        except Exception as exc:
            _log(f"[warmup] occupancy {site}/{room} failed: {exc}")
    return ok


def rebuild_all_room_panels(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rebuild tree + every room panel from NetBox (after ODF/topology changes)."""
    from field_browser import (
        get_location_tree,
        get_room_port_panel,
        invalidate_odf_records_cache,
        list_tree_rooms,
    )

    cfg = cfg or load_config()
    client = client_from_config(cfg)
    web_base = _web_base(cfg)

    _set_progress(phase="tree", label="正在刷新机房树…", i=0, n=0, current="")
    invalidate_odf_records_cache()
    tree = get_location_tree(client, force_refresh=True)

    _set_progress(phase="fp_index", label="正在从 NetBox 拉取占用索引…")
    build_fp_circuit_index(client, web_base, force_refresh=True)

    rooms = list_tree_rooms(tree)
    n = len(rooms)
    ok = 0
    failed = 0
    for i, (site, room) in enumerate(rooms, 1):
        yield_to_interactive()
        _progress_room(i, n, site, room, phase="rooms")
        _log(f"[warmup] rebuild {i}/{n} {site}/{room}")
        try:
            get_room_port_panel(client, site, room, web_base, force_refresh=True)
            touch_fp_index_cache()
            ok += 1
        except Exception as exc:
            failed += 1
            _log(f"[warmup] rebuild {site}/{room} failed: {exc}")
    return {"rooms": n, "rebuilt": ok, "failed": failed}


def refresh_occupancy_now(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rebuild occupancy index from NetBox, then stamp every room panel."""
    cfg = cfg or load_config()
    client = client_from_config(cfg)
    web_base = _web_base(cfg)
    _set_progress(phase="fp_index", label="正在从 NetBox 拉取占用索引…", i=0, n=0, current="")
    index = build_fp_circuit_index(client, web_base, force_refresh=True)
    _set_progress(phase="rooms", label="正在更新机房占用…")
    rooms_ok = refresh_cached_room_occupancy(cfg)
    return {"fp_ports": len(index), "rooms_refreshed": rooms_ok}


def run_cache_warmup(cfg: dict[str, Any] | None = None, *, refresh_rooms: bool = False) -> dict[str, Any]:
    """Refresh fp index + FMS traces; merge occupancy (or rebuild rooms if requested)."""
    from field_browser import get_room_port_panel

    cfg = cfg or load_config()
    client = client_from_config(cfg)
    web_base = _web_base(cfg)

    _set_progress(phase="fp_index", label="正在从 NetBox 拉取占用索引…", i=0, n=0, current="")
    index = build_fp_circuit_index(client, web_base, force_refresh=True)
    path_ids = collect_path_ids_from_index(index)
    trace_ok = 0
    n_paths = len(path_ids)
    for i, pid in enumerate(sorted(path_ids), 1):
        yield_to_interactive()
        _set_progress(phase="traces", i=i, n=n_paths, current=str(pid), label=f"光路轨迹 {i}/{n_paths}")
        try:
            warm_trace_cache(client, pid, web_base, force_refresh=False)
            trace_ok += 1
        except Exception as exc:
            _log(f"[warmup] trace path_id={pid} failed: {exc}")

    rooms_ok = 0
    if refresh_rooms:
        keys = list_warmed_room_keys()
        n = len(keys)
        for i, key in enumerate(keys, 1):
            if "|" not in key:
                continue
            site, room = key.split("|", 1)
            _progress_room(i, n, site, room)
            try:
                get_room_port_panel(client, site, room, web_base, force_refresh=True)
                rooms_ok += 1
            except Exception as exc:
                _log(f"[warmup] room {site}/{room} failed: {exc}")
    else:
        rooms_ok = refresh_cached_room_occupancy(cfg)

    summary = {
        "fp_ports": len(index),
        "path_ids": len(path_ids),
        "traces_warmed": trace_ok,
        "rooms_refreshed": rooms_ok,
    }
    _log(f"[warmup] done: {summary}")
    return summary


def request_cache_refresh(cfg: dict[str, Any] | None = None, *, mode: str = "occupancy") -> dict[str, Any]:
    """Start a background refresh. Returns immediately with current progress."""
    mode = _normalize_mode(mode)
    cfg = cfg or load_config()
    label = "正在更新占用…" if mode == "occupancy" else "正在全量重建机房缓存…"
    if not _try_begin(mode, label):
        prog = warmup_progress()
        return {
            "accepted": False,
            "message": "已有刷新在进行",
            **prog,
        }

    def _run() -> None:
        try:
            if mode == "full":
                summary = rebuild_all_room_panels(cfg)
            else:
                summary = refresh_occupancy_now(cfg)
            _finish(summary=summary)
            _log(f"[warmup] manual {mode} done: {summary}")
        except Exception as exc:
            traceback.print_exc()
            _finish(error=str(exc))
            _log(f"[warmup] manual {mode} failed: {exc}")

    threading.Thread(target=_run, daemon=True, name=f"cache-refresh-{mode}").start()
    return {"accepted": True, "message": "已开始刷新", **warmup_progress()}


def start_background_warmup(cfg: dict[str, Any] | None = None) -> threading.Thread | None:
    if os.environ.get("FIELD_CACHE_WARMUP", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    cfg = cfg or load_config()

    def _loop() -> None:
        time.sleep(1)
        _try_begin("startup", "启动预热…")
        try:
            client = client_from_config(cfg)
            web_base = _web_base(cfg)
            _set_progress(phase="fp_index", label="正在加载占用索引…")
            build_fp_circuit_index(client, web_base, force_refresh=False)
            _log("[warmup] fp index loaded")
        except Exception:
            traceback.print_exc()
        try:
            summary = warm_all_room_panels(cfg)
            _finish(summary=summary)
        except Exception as exc:
            traceback.print_exc()
            _finish(error=str(exc))
        while True:
            time.sleep(WARMUP_INTERVAL_SEC)
            if not _try_begin("occupancy", "定时更新占用…"):
                _log("[warmup] skip interval: refresh already running")
                continue
            try:
                refresh_rooms = os.environ.get("FIELD_WARMUP_ROOMS", "").strip().lower() in (
                    "1",
                    "true",
                    "yes",
                )
                if refresh_rooms:
                    summary = run_cache_warmup(cfg, refresh_rooms=True)
                else:
                    summary = refresh_occupancy_now(cfg)
                _finish(summary=summary)
            except Exception as exc:
                traceback.print_exc()
                _finish(error=str(exc))

    t = threading.Thread(target=_loop, daemon=True, name="field-cache-warmup")
    t.start()
    _log(f"[warmup] background thread started (interval={WARMUP_INTERVAL_SEC}s)")
    return t
