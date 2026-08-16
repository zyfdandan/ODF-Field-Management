#!/usr/bin/env python3
"""Minimal audit log for Field Portal (JSONL on disk)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_FILE = _LOG_DIR / "audit.jsonl"
_MAX_READ = 2000
# Prefer China local time so the viewer matches operator clocks.
try:
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    _TZ = timezone(timedelta(hours=8), name="CST")


def _now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def client_ip(handler) -> str:
    """Best-effort client IP (prefer proxy headers)."""
    xf = (handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if xf:
        return xf
    xr = (handler.headers.get("X-Real-IP") or "").strip()
    if xr:
        return xr
    try:
        return handler.client_address[0]
    except Exception:
        return ""


def device_from_headers(handler, extra: dict | None = None) -> dict[str, Any]:
    ua = (handler.headers.get("User-Agent") or "").strip()
    lang = (handler.headers.get("Accept-Language") or "").split(",")[0].strip()
    info: dict[str, Any] = {
        "user_agent": ua[:500],
        "accept_language": lang[:80],
    }
    if extra:
        for k in ("platform", "language", "screen", "timezone", "vendor"):
            v = extra.get(k)
            if v not in (None, ""):
                info[k] = str(v)[:120]
    return info


def write_audit(
    *,
    action: str,
    ip: str = "",
    path: str = "",
    method: str = "",
    device: dict | None = None,
    detail: dict | None = None,
    ok: bool = True,
    error: str = "",
) -> None:
    rec = {
        "ts": _now_iso(),
        "action": action,
        "ok": bool(ok),
        "ip": ip or "",
        "method": method or "",
        "path": path or "",
        "device": device or {},
        "detail": detail or {},
    }
    if error:
        rec["error"] = str(error)[:500]
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _LOCK:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)


def read_audit(
    *,
    limit: int = 200,
    action: str = "",
    ip: str = "",
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 200), 1000))
    if not _LOG_FILE.exists():
        return []
    rows: list[dict[str, Any]] = []
    with _LOCK:
        try:
            raw = _LOG_FILE.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
    lines = raw.splitlines()
    # read from end
    for line in reversed(lines[-_MAX_READ:]):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if action and str(rec.get("action") or "") != action:
            continue
        if ip and ip not in str(rec.get("ip") or ""):
            continue
        rows.append(rec)
        if len(rows) >= limit:
            break
    return rows


def log_path() -> Path:
    return _LOG_FILE
