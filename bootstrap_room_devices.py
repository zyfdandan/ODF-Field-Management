#!/usr/bin/env python3
"""Create one 48-port switch + 10 optical transceivers per server room (location)."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from bootstrap_from_sheet import _get_or_create
from import_from_excel import NetBoxClient


def paginate(client: NetBoxClient, path: str, params: dict | None = None) -> list[dict[str, Any]]:
    params = dict(params or {})
    params.setdefault("limit", 200)
    out: list[dict[str, Any]] = []
    while True:
        page = client.request("GET", path, params=params)
        out.extend(page.get("results") or [])
        nxt = page.get("next")
        if not nxt:
            break
        params = dict(params)
        params["offset"] = len(out)
    return out


def load_client() -> NetBoxClient:
    cfg_path = Path(__file__).resolve().parent / "netbox_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return NetBoxClient(cfg["base_url"], cfg["token"], verify_ssl=cfg.get("verify_ssl", True))


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", (s or "").strip().lower())
    return re.sub(r"-+", "-", s).strip("-")[:50] or "room"


def _ensure_device_type(client: NetBoxClient, mfg_id: int, model: str, u_height: float = 1.0) -> dict[str, Any]:
    slug = _slug(model)
    hits = client.request("GET", "/dcim/device-types/", params={"model": model, "limit": 5})
    if hits.get("results"):
        return hits["results"][0]
    try:
        return client.request(
            "POST",
            "/dcim/device-types/",
            json={"manufacturer": mfg_id, "model": model, "slug": slug, "u_height": u_height},
        )
    except RuntimeError:
        hits = client.request("GET", "/dcim/device-types/", params={"slug": slug, "limit": 5})
        if hits.get("results"):
            return hits["results"][0]
        raise


def _ensure_role(client: NetBoxClient, name: str, slug: str, color: str = "9e9e9e") -> dict[str, Any]:
    return _get_or_create(
        client,
        "/dcim/device-roles/",
        {"name": name},
        {"name": name, "slug": slug, "color": color},
    )


def _device_by_name(client: NetBoxClient, name: str) -> dict[str, Any] | None:
    hits = client.request("GET", "/dcim/devices/", params={"name": name, "limit": 1})
    return hits["results"][0] if hits.get("results") else None


def _ensure_device(
    client: NetBoxClient,
    *,
    name: str,
    site_id: int,
    location_id: int,
    dtype_id: int,
    role_id: int,
    dry_run: bool,
) -> dict[str, Any] | None:
    existing = _device_by_name(client, name)
    if existing:
        return existing
    if dry_run:
        print(f"  [dry-run] 创建设备: {name}")
        return None
    return client.request(
        "POST",
        "/dcim/devices/",
        json={
            "name": name,
            "site": site_id,
            "location": location_id,
            "device_type": dtype_id,
            "role": role_id,
            "status": "active",
        },
    )


def _iface_names(client: NetBoxClient, device_id: int) -> set[str]:
    names: set[str] = set()
    for iface in paginate(client, "/dcim/interfaces/", {"device_id": device_id}):
        n = (iface.get("name") or "").strip()
        if n:
            names.add(n)
    return names


def _ensure_interfaces(
    client: NetBoxClient,
    device_id: int,
    specs: list[tuple[str, str]],
    *,
    dry_run: bool,
) -> int:
    existing = _iface_names(client, device_id)
    created = 0
    for name, typ in specs:
        if name in existing:
            continue
        if dry_run:
            created += 1
            continue
        client.request(
            "POST",
            "/dcim/interfaces/",
            json={"device": device_id, "name": name, "type": typ, "enabled": True},
        )
        created += 1
    return created


def _switch_iface_specs() -> list[tuple[str, str]]:
    # 40 电口 + 8 光口（SFP），共 48 口
    specs: list[tuple[str, str]] = [(f"GE{i}", "1000base-t") for i in range(1, 41)]
    specs.extend((f"SFP{i}", "1000base-sx") for i in range(1, 9))
    return specs


def _otr_iface_specs() -> list[tuple[str, str]]:
    return [("SFP1", "1000base-sx"), ("SFP2", "1000base-sx")]


def collect_rooms(client: NetBoxClient) -> list[dict[str, Any]]:
    from naming_rules import is_odf_device_name

    rooms: dict[int, dict[str, Any]] = {}
    for d in paginate(client, "/dcim/devices/"):
        name = d.get("name") or ""
        if not is_odf_device_name(name):
            continue
        loc = d.get("location") or {}
        if not isinstance(loc, dict) or not loc.get("id"):
            continue
        loc_id = int(loc["id"])
        loc_name = (loc.get("name") or "").strip()
        site = d.get("site") or {}
        site_id = site.get("id") if isinstance(site, dict) else site
        site_name = site.get("name", "") if isinstance(site, dict) else ""
        if not loc_name or not site_id:
            continue
        rooms[loc_id] = {
            "location_id": loc_id,
            "location": loc_name,
            "site_id": int(site_id),
            "site": site_name,
        }
    return sorted(rooms.values(), key=lambda x: (x["site"], x["location"]))


def bootstrap_room_devices(client: NetBoxClient, *, dry_run: bool = False, transceiver_count: int = 10) -> None:
    mfg = _get_or_create(
        client, "/dcim/manufacturers/", {"name": "Generic"}, {"name": "Generic", "slug": "generic"}
    )
    sw_dtype = _ensure_device_type(client, mfg["id"], "Switch-48", u_height=1.0)
    otr_dtype = _ensure_device_type(client, mfg["id"], "Optical-Transceiver", u_height=1.0)
    sw_role = _ensure_role(client, "Switch", "switch", "4caf50")
    otr_role = _ensure_role(client, "光收发", "optical-transceiver", "ff9800")

    rooms = collect_rooms(client)
    print(f"==> 机房 {len(rooms)} 个，每机房 1×Switch-48 + {transceiver_count}×光收发")
    sw_specs = _switch_iface_specs()
    otr_specs = _otr_iface_specs()

    for room in rooms:
        loc = room["location"]
        print(f"\n[{room['site']} / {loc}]")
        sw_name = f"{loc}-SW48"
        sw = _ensure_device(
            client,
            name=sw_name,
            site_id=room["site_id"],
            location_id=room["location_id"],
            dtype_id=sw_dtype["id"],
            role_id=sw_role["id"],
            dry_run=dry_run,
        )
        if sw:
            n = _ensure_interfaces(client, sw["id"], sw_specs, dry_run=dry_run)
            if n:
                print(f"  交换机 {sw_name}: +{n} 接口")
            else:
                print(f"  交换机 {sw_name}: 已有")

        for i in range(1, transceiver_count + 1):
            otr_name = f"{loc}-OTR-{i:02d}"
            otr = _ensure_device(
                client,
                name=otr_name,
                site_id=room["site_id"],
                location_id=room["location_id"],
                dtype_id=otr_dtype["id"],
                role_id=otr_role["id"],
                dry_run=dry_run,
            )
            if otr:
                n = _ensure_interfaces(client, otr["id"], otr_specs, dry_run=dry_run)
                if n:
                    print(f"  光收发 {otr_name}: +{n} 接口")
                elif i == 1:
                    print(f"  光收发 {otr_name}: 已有")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap switch + optical transceivers per room")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--count", type=int, default=10, help="Optical transceivers per room")
    args = parser.parse_args()
    client = load_client()
    bootstrap_room_devices(client, dry_run=args.dry_run, transceiver_count=args.count)


if __name__ == "__main__":
    main()
