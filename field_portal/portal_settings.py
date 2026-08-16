#!/usr/bin/env python3
"""Portal UI policy: route whitelist and display names."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PORTAL_CONFIG = Path(__file__).with_name("portal_config.json")


def portal_config() -> dict[str, Any]:
    if PORTAL_CONFIG.exists():
        return json.loads(PORTAL_CONFIG.read_text(encoding="utf-8"))
    return {}


def allowed_route_keys() -> set[str] | None:
    keys = portal_config().get("allowed_route_keys")
    if not keys:
        return None
    return {str(k) for k in keys}


def route_display_overrides() -> dict[str, str]:
    raw = portal_config().get("route_display_names") or {}
    return {str(k): str(v) for k, v in raw.items()}


from naming_rules import cable_label_variants


def _route_key(odf: str, cable: str) -> str:
    return f"{odf}|{cable}" if cable else (odf or "")


def _match_allowed_route(odf: str, cable: str, allowed: set[str] | None) -> str | None:
    """Return whitelisted route_key for this ODF/cable, accepting mirror cable labels."""
    if allowed is None:
        return _route_key(odf, cable)
    for cab in cable_label_variants(cable):
        rk = _route_key(odf, cab)
        if rk in allowed:
            return rk
    rk = _route_key(odf, cable)
    return rk if rk in allowed else None


def apply_route_policy(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = allowed_route_keys()
    overrides = route_display_overrides()
    out: list[dict[str, Any]] = []
    for e in entries:
        odf = e.get("name") or ""
        cab = e.get("cable") or ""
        matched = _match_allowed_route(odf, cab, allowed)
        if allowed is not None and not matched:
            continue
        item = dict(e)
        if matched:
            item["route_key"] = matched
        rk = item.get("route_key") or _route_key(odf, cab)
        if rk in overrides:
            item["display_name"] = overrides[rk]
        out.append(item)
    return sorted(out, key=lambda x: x.get("display_name", ""))


def invalidate_tree_cache() -> None:
    try:
        from field_cache import invalidate_tree_disk

        invalidate_tree_disk()
    except Exception:
        pass
    try:
        from field_browser import _TREE_CACHE

        _TREE_CACHE["data"] = None
        _TREE_CACHE["ts"] = 0.0
    except Exception:
        pass
