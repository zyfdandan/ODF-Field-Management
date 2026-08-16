#!/usr/bin/env python3
"""Create NetBox DCIM + FMS infrastructure from unified sheet rows."""

from __future__ import annotations

import re

from typing import Any

DEFAULT_STRAND_COUNT = 12
DEFAULT_DEVICE_ROLE = "ODF"

from naming_rules import location_slug, mirror_cable_label, site_slug


def _get_or_create(client: Any, path: str, params: dict, create: dict, key: str = "name") -> dict:
    hits = client.request("GET", path, params={**params, "limit": 20})
    for item in hits.get("results", []):
        if item.get(key) == create.get(key):
            return item
    slug = create.get("slug")
    if slug:
        for item in hits.get("results", []):
            if item.get("slug") == slug:
                return item
        hits2 = client.request("GET", path, params={"slug": slug, "limit": 5})
        if hits2.get("results"):
            return hits2["results"][0]
    try:
        return client.request("POST", path, json=create)
    except RuntimeError as e:
        if "400" not in str(e):
            raise
        hits = client.request("GET", path, params={"limit": 50})
        for item in hits.get("results", []):
            if item.get(key) == create.get(key) or (slug and item.get("slug") == slug):
                return item
        raise


def _parse_trunk_note(note: str) -> tuple[str, str]:
    """芯数48 长度1800m -> ('48', '1800')"""
    note = (note or "").strip()
    strands = ""
    length = ""
    m = re.search(r"芯数\s*(\d+)", note)
    if m:
        strands = m.group(1)
    m = re.search(r"长度\s*(\d+)\s*m", note)
    if m:
        length = m.group(1)
    return strands, length


def _int_strands(row: dict[str, str]) -> int:
    try:
        sc = int(row.get("strands") or DEFAULT_STRAND_COUNT)
    except (TypeError, ValueError):
        sc = DEFAULT_STRAND_COUNT
    if not row.get("strands"):
        parsed, _ = _parse_trunk_note(row.get("note", ""))
        if parsed:
            try:
                sc = int(parsed)
            except ValueError:
                pass
    return max(1, sc)


def _odf_strands_from_trunks(trunks: list[dict[str, str]]) -> dict[str, int]:
    """缆段芯数 -> 两端 ODF 设备型号/端口容量（ODF-{芯数}）。"""
    out: dict[str, int] = {}
    for row in trunks:
        sc = _int_strands(row)
        for dev in (row.get("dev_a", ""), row.get("dev_b", "")):
            if dev:
                out[dev.strip()] = sc
    return out


def _ensure_odf_device_type(client: Any, mfg_id: int, strand_count: int, cache: dict[int, int]) -> dict:
    strand_count = max(1, int(strand_count))
    if strand_count in cache:
        dtype_id = cache[strand_count]
        return {"id": dtype_id, "model": f"ODF-{strand_count}"}
    model = f"ODF-{strand_count}"
    slug = f"odf-{strand_count}"
    hits = client.request("GET", "/dcim/device-types/", params={"model": model, "limit": 5})
    if hits.get("results"):
        dtype = hits["results"][0]
    else:
        try:
            dtype = client.request(
                "POST",
                "/dcim/device-types/",
                json={"manufacturer": mfg_id, "model": model, "slug": slug, "u_height": 1.0},
            )
            print(f"  ODF 型号: {model}")
        except RuntimeError as exc:
            if "400" not in str(exc):
                raise
            hits = client.request("GET", "/dcim/device-types/", params={"slug": slug, "limit": 5})
            if not hits.get("results"):
                hits = client.request("GET", "/dcim/device-types/", params={"limit": 200})
                for item in hits.get("results", []):
                    if item.get("model") == model:
                        dtype = item
                        break
                else:
                    raise
            else:
                dtype = hits["results"][0]
    cache[strand_count] = dtype["id"]
    return dtype


def _ensure_fiber_cable_type(client: Any, mfg_id: int, strand_count: int, cache: dict[int, int]) -> int:
    strand_count = max(1, int(strand_count))
    if strand_count in cache:
        return cache[strand_count]
    model = f"G.652D-{strand_count}"
    hits = client.request("GET", "/plugins/fms/cable-types/", params={"model": model, "limit": 1})
    if hits.get("results"):
        fct_id = hits["results"][0]["id"]
    else:
        fct = client.request(
            "POST",
            "/plugins/fms/cable-types/",
            json={
                "manufacturer": mfg_id,
                "model": model,
                "construction": "loose_tube",
                "strand_count": strand_count,
                "is_armored": False,
            },
        )
        fct_id = fct["id"]
        print(f"  光缆类型: {model}")
    cache[strand_count] = fct_id
    return fct_id


def _ensure_rear_port(client: Any, device_id: int, name: str, positions: int) -> int:
    hits = client.request("GET", "/dcim/rear-ports/", params={"device_id": device_id, "name": name, "limit": 5})
    if hits.get("results"):
        rp = hits["results"][0]
        if int(rp.get("positions") or 0) != int(positions):
            client.request("PATCH", f"/dcim/rear-ports/{rp['id']}/", json={"positions": int(positions)})
        return rp["id"]
    rp = client.request(
        "POST",
        "/dcim/rear-ports/",
        json={"device": device_id, "name": name, "type": "splice", "positions": int(positions)},
    )
    return rp["id"]


def _ensure_location(client: Any, site_id: int, name: str, cache: dict[str, int]) -> int:
    name = (name or "").strip()
    if not name:
        raise RuntimeError("机房名称为空")
    if name in cache:
        return cache[name]
    hits = client.request("GET", "/dcim/locations/", params={"site_id": site_id, "name": name, "limit": 5})
    if hits.get("results"):
        cache[name] = hits["results"][0]["id"]
        return cache[name]
    slug = location_slug(name)
    try:
        loc = client.request(
            "POST",
            "/dcim/locations/",
            json={"site": site_id, "name": name, "slug": slug, "status": "active"},
        )
    except RuntimeError as exc:
        if "400" not in str(exc):
            raise
        hits = client.request("GET", "/dcim/locations/", params={"site_id": site_id, "slug": slug, "limit": 1})
        if hits.get("results"):
            loc = hits["results"][0]
        else:
            raise
    cache[name] = loc["id"]
    print(f"  机房: {name}")
    return cache[name]


def apply_infra_from_sheet(
    client: Any,
    sites: list[dict[str, str]],
    devices: list[dict[str, str]],
    trunks: list[dict[str, str]],
    *,
    locations: list[dict[str, str]] | None = None,
    dry_run: bool = False,
) -> None:
    locations = locations or []
    if not sites and not locations and not devices and not trunks:
        return

    if dry_run:
        print(
            f"  DRY-RUN 基础设施: 站点 {len(sites)}, 机房 {len(locations)}, "
            f"ODF {len(devices)}, 缆段 {len(trunks)}"
        )
        return

    print(
        f"\n==> 单表基础设施（站点 {len(sites)} / 机房 {len(locations)} / "
        f"ODF {len(devices)} / 缆段 {len(trunks)}）"
    )

    mfg = _get_or_create(
        client, "/dcim/manufacturers/", {"name": "Generic"}, {"name": "Generic", "slug": "generic"}
    )
    role = _get_or_create(
        client,
        "/dcim/device-roles/",
        {"name": DEFAULT_DEVICE_ROLE},
        {"name": DEFAULT_DEVICE_ROLE, "slug": "odf", "color": "2196f3"},
    )
    dtype_cache: dict[int, int] = {}
    odf_strands = _odf_strands_from_trunks(trunks)

    site_ids: dict[str, int] = {}
    for row in sites:
        name = row["name"]
        slug = row.get("slug") or site_slug(name)
        payload: dict[str, Any] = {"name": name, "status": "active", "slug": slug}
        if row.get("address"):
            payload["physical_address"] = row["address"]
        if row.get("latitude"):
            payload["latitude"] = float(row["latitude"])
        if row.get("longitude"):
            payload["longitude"] = float(row["longitude"])
        site = _get_or_create(client, "/dcim/sites/", {"name": name}, payload)
        site_ids[name] = site["id"]
        print(f"  站点: {name}")

    location_ids: dict[str, int] = {}
    for row in locations:
        loc_name = (row.get("name") or "").strip()
        site_name = row.get("site") or (sites[0]["name"] if sites else "")
        if not loc_name or not site_name:
            continue
        if site_name not in site_ids:
            site = _get_or_create(
                client,
                "/dcim/sites/",
                {"name": site_name},
                {"name": site_name, "status": "active", "slug": site_slug(site_name)},
            )
            site_ids[site_name] = site["id"]
        _ensure_location(client, site_ids[site_name], loc_name, location_ids)

    dev_ids: dict[str, int] = {}
    for row in devices:
        name = row["name"]
        strand_count = odf_strands.get(name, DEFAULT_STRAND_COUNT)
        if row.get("strands"):
            try:
                strand_count = max(1, int(row["strands"]))
            except (TypeError, ValueError):
                pass
        elif row.get("model", "").upper().startswith("ODF-"):
            try:
                strand_count = max(1, int(row["model"].split("-", 1)[1]))
            except (IndexError, ValueError):
                pass
        dtype = _ensure_odf_device_type(client, mfg["id"], strand_count, dtype_cache)

        hits = client.request("GET", "/dcim/devices/", params={"name": name, "limit": 1})
        if hits.get("results"):
            dev = hits["results"][0]
            dev_ids[name] = dev["id"]
            cur = dev.get("device_type") or {}
            cur_id = cur["id"] if isinstance(cur, dict) else cur
            if cur_id != dtype["id"]:
                client.request("PATCH", f"/dcim/devices/{dev['id']}/", json={"device_type": dtype["id"]})
                print(f"  ODF 更新型号: {name} -> ODF-{strand_count}")
            else:
                print(f"  ODF 已有: {name} (ODF-{strand_count})")
            continue
        site_name = row.get("site") or (sites[0]["name"] if sites else "")
        if site_name not in site_ids:
            site = _get_or_create(
                client,
                "/dcim/sites/",
                {"name": site_name},
                {"name": site_name, "status": "active", "slug": site_name[:50]},
            )
            site_ids[site_name] = site["id"]
        payload = {
            "name": name,
            "site": site_ids[site_name],
            "device_type": dtype["id"],
            "role": role["id"],
            "status": "active",
        }
        loc_name = (row.get("location") or "").strip()
        if loc_name:
            payload["location"] = _ensure_location(client, site_ids[site_name], loc_name, location_ids)
        elif row.get("location"):
            payload["comments"] = f"机房: {row['location']}"
        client.request("POST", "/dcim/devices/", json=payload)
        dev_ids[name] = client.request("GET", "/dcim/devices/", params={"name": name, "limit": 1})["results"][0]["id"]
        print(f"  ODF 新建: {name} (ODF-{strand_count})")

    fct_cache: dict[int, int] = {}

    for row in trunks:
        label = row["label"]
        dev_a, dev_b = row["dev_a"], row["dev_b"]
        strand_count = _int_strands(row)
        if not row.get("length"):
            _, parsed_len = _parse_trunk_note(row.get("note", ""))
            if parsed_len:
                row["length"] = parsed_len

        fct_id = _ensure_fiber_cable_type(client, mfg["id"], strand_count, fct_cache)

        for dn in (dev_a, dev_b):
            if dn not in dev_ids:
                hits = client.request("GET", "/dcim/devices/", params={"name": dn, "limit": 1})
                if not hits.get("results"):
                    raise RuntimeError(f"缆段 {label} 引用未知 ODF: {dn}（请先在表中登记 ODF 行）")
                dev_ids[dn] = hits["results"][0]["id"]

        hits = client.request("GET", "/dcim/cables/", params={"label": label, "limit": 1})
        if hits.get("results"):
            cable_id = hits["results"][0]["id"]
        else:
            mirror = mirror_cable_label(label)
            rp_a = _ensure_rear_port(client, dev_ids[dev_a], f"TMP-{label}-A", strand_count)
            rp_b = _ensure_rear_port(client, dev_ids[dev_b], f"TMP-{mirror}-B", strand_count)
            cab_payload: dict[str, Any] = {
                "type": "smf",
                "status": "connected",
                "label": label,
                "a_terminations": [{"object_type": "dcim.rearport", "object_id": rp_a}],
                "b_terminations": [{"object_type": "dcim.rearport", "object_id": rp_b}],
            }
            if row.get("length"):
                try:
                    cab_payload["length"] = float(row["length"])
                    cab_payload["length_unit"] = "m"
                except ValueError:
                    pass
            c = client.request("POST", "/dcim/cables/", json=cab_payload)
            cable_id = c["id"]
            print(f"  缆段: {label}")

        fc_hits = client.request("GET", "/plugins/fms/fiber-cables/", params={"cable_id": cable_id, "limit": 1})
        if fc_hits.get("results"):
            fc_id = fc_hits["results"][0]["id"]
            fc_type = fc_hits["results"][0].get("fiber_cable_type") or {}
            cur_type_id = fc_type["id"] if isinstance(fc_type, dict) else fc_type
            if cur_type_id != fct_id:
                client.request("PATCH", f"/plugins/fms/fiber-cables/{fc_id}/", json={"fiber_cable_type": fct_id})
        else:
            fc = client.request(
                "POST", "/plugins/fms/fiber-cables/", json={"cable": cable_id, "fiber_cable_type": fct_id}
            )
            fc_id = fc["id"]

    print("  （FMS 端口由 bootstrap_test_infra / FiberInfra.provision_cable 配置）")
