#!/usr/bin/env python3
"""Persist ODF frame display order per room (simulates cabinet install sequence)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LAYOUT_FILE = Path(__file__).with_name("odf_room_layout.json")


def room_key(site: str, room: str) -> str:
    return f"{site.strip()}|{room.strip()}"


def _load_all() -> dict[str, list[str]]:
    if not LAYOUT_FILE.exists():
        return {}
    try:
        data = json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    rooms = data.get("rooms") or {}
    out: dict[str, list[str]] = {}
    for k, v in rooms.items():
        if isinstance(v, list):
            out[str(k)] = [str(x) for x in v if x]
    return out


def _save_all(data: dict[str, list[str]]) -> None:
    LAYOUT_FILE.write_text(
        json.dumps({"rooms": data}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_room_order(site: str, room: str) -> list[str]:
    return list(_load_all().get(room_key(site, room), []))


def save_room_order(site: str, room: str, order: list[str]) -> list[str]:
    key = room_key(site, room)
    cleaned = []
    seen: set[str] = set()
    for rk in order:
        rk = str(rk).strip()
        if not rk or rk in seen:
            continue
        seen.add(rk)
        cleaned.append(rk)
    all_data = _load_all()
    all_data[key] = cleaned
    _save_all(all_data)
    return cleaned


def sort_routes_by_layout(routes: list[dict[str, Any]], site: str, room: str) -> list[dict[str, Any]]:
    order = get_room_order(site, room)
    if not order:
        return routes
    by_key: dict[str, dict[str, Any]] = {}
    for r in routes:
        rk = str(r.get("route_key") or "")
        if rk:
            by_key[rk] = r
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rk in order:
        if rk in by_key:
            out.append(by_key[rk])
            seen.add(rk)
    for r in routes:
        rk = str(r.get("route_key") or "")
        if rk not in seen:
            out.append(r)
    return out
