"""Simple trunk cable submission: pick rooms + cores/length → auto ODF/cable names → NetBox."""

from __future__ import annotations

from typing import Any

from bootstrap_from_sheet import apply_infra_from_sheet
from fiber_infra import FiberInfra
from naming_rules import (
    ROUTE_SUFFIXES,
    asset_room_name,
    format_cable_label,
    format_odf_device_name,
    port_name_for_cable_fiber,
)

from field_service import paginate


# 业务别名 → NetBox 已有机房。不要把「烧结白灰主控楼」规范化成「白灰窑机房」。
_ROOM_LOCATION_ALIASES = {
    "白灰窑机房": "烧结白灰主控楼",
    "白灰窑": "烧结白灰主控楼",
    "烧结白灰主控": "烧结白灰主控楼",
}


def _location_exists(client, name: str, site: str = "") -> bool:
    name = (name or "").strip()
    if not name:
        return False
    hits = client.request("GET", "/dcim/locations/", params={"name": name, "limit": 50})
    for loc in hits.get("results") or []:
        if loc.get("name") != name:
            continue
        loc_site = ((loc.get("site") or {}).get("name") or "") if isinstance(loc.get("site"), dict) else ""
        if not site or loc_site == site:
            return True
    return False


def _room_name(client, raw: str, *, is_new: bool, new_name: str = "", site: str = "") -> str:
    name = asset_room_name(new_name if is_new else raw)
    if not name:
        raise ValueError("请填写或选择机房")
    canonical = _ROOM_LOCATION_ALIASES.get(name)
    if canonical and _location_exists(client, canonical, site):
        return canonical
    if _location_exists(client, name, site):
        return name
    return name


def cable_exists(client, label: str) -> bool:
    label = (label or "").strip()
    if not label:
        return False
    hits = client.request("GET", "/dcim/cables/", params={"label": label, "limit": 1})
    return bool(hits.get("results"))


_CONNECTOR_CHOICES = ("FC", "SC")
_CONNECTOR_LABELS = {"FC": "圆口-FC", "SC": "大方-SC"}


def _normalize_connector(val: str, *, required: bool = False) -> str:
    raw = (val or "").strip()
    if not raw:
        if required:
            raise ValueError("请选择 ODF 接口类型（圆口-FC 或 大方-SC）")
        return ""
    if "大方" in raw:
        return "SC"
    if "圆口" in raw:
        return "FC"
    compact = raw.upper().replace("接口", "")
    for ch in ("-", "_", " ", ":", "："):
        compact = compact.replace(ch, "")
    if compact in _CONNECTOR_CHOICES:
        return compact
    if compact.endswith("SC") and "FC" not in compact:
        return "SC"
    if compact.endswith("FC"):
        return "FC"
    if required:
        raise ValueError("请选择 ODF 接口类型（圆口-FC 或 大方-SC）")
    return ""


def _connector_label(code: str) -> str:
    return _CONNECTOR_LABELS.get(code, code or "")


def _tag_is_connector(tag: dict) -> bool:
    if not isinstance(tag, dict):
        return False
    for field in ("name", "slug", "display"):
        if _normalize_connector(tag.get(field) or "", required=False):
            return True
    return False


def _ensure_connector_tag(client, connector: str) -> int:
    """Use existing NetBox tags 圆口-FC / 大方-SC (slug fc/sc). Do not create FC/SC aliases."""
    connector = _normalize_connector(connector, required=True)
    slug = connector.lower()
    label = _CONNECTOR_LABELS[connector]
    hits = client.request("GET", "/extras/tags/", params={"slug": slug, "limit": 20})
    for t in hits.get("results") or []:
        if (t.get("slug") or "").lower() == slug:
            return int(t["id"])
    hits = client.request("GET", "/extras/tags/", params={"name": label, "limit": 20})
    for t in hits.get("results") or []:
        if (t.get("name") or "").strip() == label:
            return int(t["id"])
    raise ValueError(f"NetBox 中找不到接口标签「{label}」，请先在后台创建后再选")


def list_connector_options(client) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for t in paginate(client, "/extras/tags/"):
        code = _normalize_connector(t.get("slug") or "") or _normalize_connector(t.get("name") or "")
        if not code or code in found:
            continue
        found[code] = {
            "value": code,
            "label": (t.get("name") or "").strip() or _CONNECTOR_LABELS[code],
        }
    out = [found[c] for c in _CONNECTOR_CHOICES if c in found]
    if out:
        return out
    return [{"value": c, "label": _CONNECTOR_LABELS[c]} for c in _CONNECTOR_CHOICES]


def _set_device_connector(client, device_name: str, connector: str, *, device: dict | None = None) -> bool:
    """Apply 圆口-FC / 大方-SC tag. Returns True if NetBox was patched."""
    connector = _normalize_connector(connector, required=True)
    dev = device
    if not dev:
        hits = client.request("GET", "/dcim/devices/", params={"name": device_name, "limit": 10})
        results = hits.get("results") or []
        if not results:
            raise RuntimeError(f"找不到 ODF：{device_name}")
        dev = results[0]
    tag_id = _ensure_connector_tag(client, connector)
    old_ids: list[int] = []
    keep: list[int] = []
    for t in dev.get("tags") or []:
        if isinstance(t, dict):
            old_ids.append(int(t["id"]))
            if _tag_is_connector(t):
                continue
            keep.append(int(t["id"]))
        elif isinstance(t, int):
            old_ids.append(int(t))
            keep.append(int(t))
    if tag_id not in keep:
        keep.append(tag_id)
    if set(keep) == set(old_ids):
        return False
    client.request("PATCH", f"/dcim/devices/{dev['id']}/", json={"tags": keep})
    return True


def set_odf_connector(client, payload: dict[str, Any]) -> dict[str, Any]:
    odf = (payload.get("odf") or payload.get("device") or "").strip()
    connector = _normalize_connector(
        str(payload.get("connector") or payload.get("connector_type") or ""),
        required=True,
    )
    if not odf:
        raise ValueError("缺少 ODF 名称")
    _set_device_connector(client, odf, connector)
    try:
        from field_browser import invalidate_odf_records_cache, invalidate_room_panel_cache

        invalidate_odf_records_cache()
        invalidate_room_panel_cache(
            site=(payload.get("site") or "").strip(),
            room=(payload.get("room") or "").strip(),
        )
    except Exception:
        pass
    return {
        "message": f"已将「{odf}」标记为 {_connector_label(connector)}",
        "odf": odf,
        "connector": connector,
        "connector_label": _connector_label(connector),
    }


def tag_all_odf_connectors(client, connector: str = "SC") -> dict[str, Any]:
    from field_service import is_odf_device

    connector = _normalize_connector(connector, required=True)
    tagged = 0
    skipped = 0
    errors: list[str] = []
    for d in paginate(client, "/dcim/devices/"):
        if not is_odf_device(d):
            continue
        name = d.get("name") or ""
        try:
            if _set_device_connector(client, name, connector, device=d):
                tagged += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    try:
        from field_browser import invalidate_odf_records_cache, invalidate_room_panel_cache

        invalidate_odf_records_cache()
        invalidate_room_panel_cache()
    except Exception:
        pass
    return {
        "message": (
            f"已标记 {tagged} 个 ODF 为 {_connector_label(connector)}，"
            f"跳过 {skipped} 个已是该标签"
        ),
        "tagged": tagged,
        "skipped": skipped,
        "errors": errors,
        "connector": connector,
        "connector_label": _connector_label(connector),
    }


def pick_cable_label(client, room_a: str, room_b: str) -> tuple[str, str]:
    """Return (cable_label, suffix) — auto 备用 when duplicate."""
    base = format_cable_label(room_a, room_b, from_room=room_a)
    if not cable_exists(client, base):
        return base, ""
    for suffix in ROUTE_SUFFIXES:
        label = format_cable_label(room_a, room_b, suffix=suffix, from_room=room_a)
        if not cable_exists(client, label):
            return label, suffix
    raise ValueError(f"「{room_a}」与「{room_b}」之间已有过多缆段，请联系管理员手动命名")


def build_trunk_plan(
    client,
    *,
    site: str,
    room_a: str,
    room_b: str,
    room_a_new: bool = False,
    room_b_new: bool = False,
    room_a_text: str = "",
    room_b_text: str = "",
    cores: int = 12,
    length_m: str = "",
    connector_a: str = "",
    connector_b: str = "",
    require_connector: bool = False,
) -> dict[str, Any]:
    site = (site or "").strip()
    if not site:
        raise ValueError("请选择站点")
    ra = _room_name(client, room_a, is_new=room_a_new, new_name=room_a_text, site=site)
    rb = _room_name(client, room_b, is_new=room_b_new, new_name=room_b_text, site=site)
    if ra == rb:
        raise ValueError("两端机房不能相同")

    try:
        cores_i = max(1, int(cores))
    except (TypeError, ValueError):
        raise ValueError("芯数无效") from None

    cable, suffix = pick_cable_label(client, ra, rb)
    odf_a = format_odf_device_name(ra, rb, suffix=suffix)
    odf_b = format_odf_device_name(rb, ra, suffix=suffix)
    fibers = [f"1-{i}" for i in range(1, cores_i + 1)]
    port_a = port_name_for_cable_fiber(cable, "1-1", local_room=ra)
    port_b = port_name_for_cable_fiber(cable, "1-1", local_room=rb)

    conn_a = _normalize_connector(connector_a, required=False)
    conn_b = _normalize_connector(connector_b, required=False)
    if conn_a and not conn_b:
        conn_b = conn_a
    if require_connector and (conn_a not in _CONNECTOR_CHOICES or conn_b not in _CONNECTOR_CHOICES):
        raise ValueError("请选择两端 ODF 的接口类型（圆口-FC 或 大方-SC）")

    note = f"芯数{cores_i}"
    if (length_m or "").strip():
        note += f" 长度{str(length_m).strip()}m"
    if conn_a or conn_b:
        note += f" 接口A{_connector_label(conn_a) or '-'} B{_connector_label(conn_b) or '-'}"

    return {
        "site": site,
        "room_a": ra,
        "room_b": rb,
        "cable": cable,
        "suffix": suffix,
        "odf_a": odf_a,
        "odf_b": odf_b,
        "cores": cores_i,
        "length_m": (length_m or "").strip(),
        "connector_a": conn_a,
        "connector_b": conn_b,
        "connector_a_label": _connector_label(conn_a),
        "connector_b_label": _connector_label(conn_b),
        "note": note,
        "fibers": fibers,
        "port_a_example": port_a,
        "port_b_example": port_b,
        "exists": cable_exists(client, cable),
    }


def list_trunk_context(client) -> dict[str, Any]:
    sites_out: list[dict[str, Any]] = []
    for site in paginate(client, "/dcim/sites/"):
        sid = site["id"]
        sname = site["name"]
        rooms: set[str] = set()
        for loc in paginate(client, "/dcim/locations/", {"site_id": sid}):
            if loc.get("name"):
                rooms.add(str(loc["name"]))
        for dev in paginate(client, "/dcim/devices/"):
            ds = dev.get("site") or {}
            if (ds.get("name") if isinstance(ds, dict) else "") != sname:
                continue
            loc = dev.get("location") or {}
            if isinstance(loc, dict) and loc.get("name"):
                rooms.add(str(loc["name"]))
        sites_out.append({"name": sname, "rooms": sorted(rooms)})
    sites_out.sort(key=lambda x: x["name"])
    return {
        "sites": sites_out,
        "core_options": [4, 8, 12, 24, 48, 96],
        "connector_options": list_connector_options(client),
    }


def submit_trunk_infra(client, payload: dict[str, Any]) -> dict[str, Any]:
    plan = build_trunk_plan(
        client,
        site=(payload.get("site") or "").strip(),
        room_a=(payload.get("room_a") or "").strip(),
        room_b=(payload.get("room_b") or "").strip(),
        room_a_new=bool(payload.get("room_a_new")),
        room_b_new=bool(payload.get("room_b_new")),
        room_a_text=(payload.get("room_a_text") or "").strip(),
        room_b_text=(payload.get("room_b_text") or "").strip(),
        cores=payload.get("cores") or 12,
        length_m=str(payload.get("length_m") or payload.get("length") or "").strip(),
        connector_a=str(payload.get("connector_a") or payload.get("conn_a") or "").strip(),
        connector_b=str(payload.get("connector_b") or payload.get("conn_b") or "").strip(),
        require_connector=True,
    )

    if cable_exists(client, plan["cable"]):
        raise ValueError(f"缆段「{plan['cable']}」已存在，请勿重复导入")

    locations = [
        {"name": plan["room_a"], "site": plan["site"], "note": ""},
        {"name": plan["room_b"], "site": plan["site"], "note": ""},
    ]
    devices = [
        {"name": plan["odf_a"], "site": plan["site"], "location": plan["room_a"], "strands": str(plan["cores"])},
        {"name": plan["odf_b"], "site": plan["site"], "location": plan["room_b"], "strands": str(plan["cores"])},
    ]
    trunks = [
        {
            "label": plan["cable"],
            "dev_a": plan["odf_a"],
            "dev_b": plan["odf_b"],
            "strands": str(plan["cores"]),
            "length": plan["length_m"],
            "note": plan["note"],
        }
    ]

    apply_infra_from_sheet(client, [], devices, trunks, locations=locations)
    _set_device_connector(client, plan["odf_a"], plan["connector_a"])
    _set_device_connector(client, plan["odf_b"], plan["connector_b"])

    infra = FiberInfra(client)
    for dev in (plan["odf_a"], plan["odf_b"]):
        infra.provision_cable(dev, plan["cable"])
    # 先校正缆段两端后端口，再统一把 :F* / 旧名改成 {A}to{B}{排}-{芯}
    infra.fix_cable_termination(plan["cable"])
    from naming_rules import cable_label_variants

    variants = cable_label_variants(plan["cable"]) or [plan["cable"]]
    for dev in (plan["odf_a"], plan["odf_b"]):
        infra.fix_device_port_names(dev)
        infra.rename_cable_front_ports(dev, variants)

    try:
        from field_browser import (
            get_location_tree,
            invalidate_odf_records_cache,
            invalidate_room_panel_cache,
        )
        from portal_settings import invalidate_tree_cache

        invalidate_odf_records_cache()
        invalidate_tree_cache()
        invalidate_room_panel_cache(site=plan["site"], room=plan["room_a"])
        invalidate_room_panel_cache(site=plan["site"], room=plan["room_b"])
        get_location_tree(client, force_refresh=True)
        tree_refreshed = True
    except Exception:
        tree_refreshed = False

    return {
        "message": "缆段已导入 NetBox",
        "plan": plan,
        "tree_refreshed": tree_refreshed,
        "netbox": {
            "cable": plan["cable"],
            "odf_a": plan["odf_a"],
            "odf_b": plan["odf_b"],
            "cores": plan["cores"],
            "fibers": plan["fibers"],
            "site": plan["site"],
            "room_a": plan["room_a"],
            "room_b": plan["room_b"],
            "connector_a": plan["connector_a"],
            "connector_b": plan["connector_b"],
            "connector_a_label": plan.get("connector_a_label"),
            "connector_b_label": plan.get("connector_b_label"),
        },
    }
