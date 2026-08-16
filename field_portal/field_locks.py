#!/usr/bin/env python3
"""Soft edit locks for ODF field portal (Plan B).

Identity = client IP + short device summary (no login accounts).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_LOCK_FILE = Path(__file__).with_name("edit_locks.json")
_TTL_SEC = 180  # 3 minutes
_RENEW_EXTEND_SEC = 180


def _now() -> float:
    return time.time()


def _read() -> dict[str, dict[str, Any]]:
    if not _LOCK_FILE.exists():
        return {}
    try:
        data = json.loads(_LOCK_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: dict[str, dict[str, Any]]) -> None:
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LOCK_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_LOCK_FILE)


def lock_key(odf: str, cable: str, fiber: str) -> str:
    return f"{(odf or '').strip()}|{(cable or '').strip()}|{(fiber or '').strip()}"


def device_summary(device: dict[str, Any] | None, user_agent: str = "") -> str:
    """Short human-readable device blurb for lock banner."""
    d = device or {}
    parts: list[str] = []
    platform = str(d.get("platform") or "").strip()
    screen = str(d.get("screen") or "").strip()
    ua = (user_agent or str(d.get("user_agent") or "")).lower()

    kind = ""
    if "android" in ua or "iphone" in ua or "ipad" in ua or "mobile" in ua:
        kind = "手机"
    elif "windows" in ua or "macintosh" in ua or "linux" in ua or "x11" in ua:
        kind = "电脑"
    elif platform:
        kind = platform[:24]
    if kind:
        parts.append(kind)
    elif platform:
        parts.append(platform[:24])
    if screen and screen not in ("0x0",):
        parts.append(screen)
    return " · ".join(parts) if parts else "未知设备"


def format_holder(rec: dict[str, Any]) -> str:
    ip = (rec.get("ip") or "未知IP").strip() or "未知IP"
    summary = (rec.get("device_summary") or "").strip()
    if summary:
        return f"{ip}（{summary}）"
    return ip


def _public(rec: dict[str, Any], *, mine: bool = False) -> dict[str, Any]:
    remain = max(0, int(float(rec.get("expires_at", 0)) - _now()))
    return {
        "locked": True,
        "mine": mine,
        "key": rec.get("key") or "",
        "odf": rec.get("odf") or "",
        "cable": rec.get("cable") or "",
        "fiber": rec.get("fiber") or "",
        "ip": rec.get("ip") or "",
        "device_summary": rec.get("device_summary") or "",
        "holder": format_holder(rec),
        "token": rec.get("token") if mine else "",
        "expires_in": remain,
        "ttl_sec": _TTL_SEC,
        "message": (
            f"你正在编辑此端口（约 {remain // 60} 分 {remain % 60} 秒后自动释放）"
            if mine
            else f"该端口正被编辑中：{format_holder(rec)} · 剩余约 {remain // 60} 分 {remain % 60} 秒"
        ),
    }


def _purge(data: dict[str, dict[str, Any]]) -> bool:
    now = _now()
    dead = [k for k, v in data.items() if float(v.get("expires_at", 0)) <= now]
    for k in dead:
        del data[k]
    return bool(dead)


def get_lock(odf: str, cable: str, fiber: str) -> dict[str, Any] | None:
    key = lock_key(odf, cable, fiber)
    with _LOCK:
        data = _read()
        if _purge(data):
            _write(data)
        rec = data.get(key)
        return dict(rec) if rec else None


def acquire_lock(
    *,
    odf: str,
    cable: str,
    fiber: str,
    ip: str,
    device: dict[str, Any] | None = None,
    user_agent: str = "",
    token: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Acquire or renew a port edit lock. Returns public lock status."""
    odf = (odf or "").strip()
    cable = (cable or "").strip()
    fiber = (fiber or "").strip()
    ip = (ip or "").strip() or "unknown"
    token = (token or "").strip()
    if not odf or not fiber:
        raise ValueError("缺少 odf 或 fiber")

    key = lock_key(odf, cable, fiber)
    summary = device_summary(device, user_agent)
    now = _now()

    with _LOCK:
        data = _read()
        _purge(data)
        cur = data.get(key)

        if cur:
            same_token = token and token == (cur.get("token") or "")
            same_ip = ip and ip == (cur.get("ip") or "")
            if same_token or (same_ip and not token):
                # renew
                if not token:
                    token = cur.get("token") or uuid.uuid4().hex
                cur["token"] = token
                cur["ip"] = ip
                cur["device_summary"] = summary or cur.get("device_summary") or ""
                cur["device"] = device or cur.get("device") or {}
                cur["expires_at"] = now + _RENEW_EXTEND_SEC
                cur["updated_at"] = now
                data[key] = cur
                _write(data)
                return _public(cur, mine=True)

            if not force:
                out = _public(cur, mine=False)
                out["ok"] = False
                out["conflict"] = True
                return out

            # force takeover
        token = token or uuid.uuid4().hex
        rec = {
            "key": key,
            "odf": odf,
            "cable": cable,
            "fiber": fiber,
            "ip": ip,
            "token": token,
            "device_summary": summary,
            "device": device or {},
            "created_at": now,
            "updated_at": now,
            "expires_at": now + _TTL_SEC,
        }
        data[key] = rec
        _write(data)
        out = _public(rec, mine=True)
        out["ok"] = True
        out["forced"] = bool(force and cur)
        return out


def renew_lock(
    *,
    odf: str,
    cable: str,
    fiber: str,
    ip: str,
    token: str,
    device: dict[str, Any] | None = None,
    user_agent: str = "",
) -> dict[str, Any]:
    return acquire_lock(
        odf=odf,
        cable=cable,
        fiber=fiber,
        ip=ip,
        device=device,
        user_agent=user_agent,
        token=token,
        force=False,
    )


def release_lock(
    *,
    odf: str,
    cable: str,
    fiber: str,
    ip: str = "",
    token: str = "",
    force: bool = False,
) -> dict[str, Any]:
    key = lock_key(odf, cable, fiber)
    with _LOCK:
        data = _read()
        _purge(data)
        cur = data.get(key)
        if not cur:
            return {"ok": True, "locked": False, "message": "锁已不存在"}
        same_token = token and token == (cur.get("token") or "")
        same_ip = ip and ip == (cur.get("ip") or "")
        if force or same_token or same_ip:
            del data[key]
            _write(data)
            return {"ok": True, "locked": False, "message": "已释放编辑锁"}
        out = _public(cur, mine=False)
        out["ok"] = False
        out["conflict"] = True
        out["message"] = f"无法释放：当前由 {format_holder(cur)} 持有"
        return out


def lock_status_public(odf: str, cable: str, fiber: str, *, ip: str = "") -> dict[str, Any]:
    rec = get_lock(odf, cable, fiber)
    if not rec:
        return {"locked": False}
    mine = bool(ip) and ip == (rec.get("ip") or "")
    return _public(rec, mine=mine)


def assert_editable(
    *,
    odf: str,
    cable: str,
    fiber: str,
    ip: str,
    token: str = "",
) -> None:
    """Raise ValueError if another holder owns the lock."""
    rec = get_lock(odf, cable, fiber)
    if not rec:
        return
    same_token = token and token == (rec.get("token") or "")
    same_ip = ip and ip == (rec.get("ip") or "")
    if same_token or same_ip:
        return
    raise ValueError(
        f"该端口正被编辑中：{format_holder(rec)}。请稍后重试，或强制接管后再提交。"
    )
