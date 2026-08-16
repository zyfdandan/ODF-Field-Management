#!/usr/bin/env python3
"""Provision multi-cable ports, patch cords, splice plans for test topology."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests

CONFIG = json.loads(Path(__file__).with_name("netbox_config.json").read_text(encoding="utf-8"))

CABLE_PREFIX = {
    "CBL-轧钢-白灰窑01": "c1",
    "CBL-白灰窑01-白灰窑02": "c2",
    "CBL-白灰窑02-烧结01": "c3",
    "CBL-白灰窑01-烧结01": "c4",
    "CBL-烧结02-白灰窑01": "c5",
    "CBL-白灰窑01-轧钢": "c6",
}

DEVICE_CABLES: dict[str, list[str]] = {
    "轧钢机房-B01-上行ODF框-01": ["CBL-轧钢-白灰窑01", "CBL-白灰窑01-轧钢"],
    "白灰窑机房-A01-上行ODF框-01": [
        "CBL-轧钢-白灰窑01",
        "CBL-白灰窑01-白灰窑02",
        "CBL-白灰窑01-烧结01",
        "CBL-烧结02-白灰窑01",
        "CBL-白灰窑01-轧钢",
    ],
    "白灰窑机房-A01-上行ODF框-02": ["CBL-白灰窑01-白灰窑02", "CBL-白灰窑02-烧结01"],
    "烧结主控机房-A01-上行ODF框-01": ["CBL-白灰窑02-烧结01", "CBL-白灰窑01-烧结01", "CBL-白灰窑01-白灰窑02"],
    "烧结主控机房-A01-上行ODF框-02": ["CBL-烧结02-白灰窑01"],
}

INTRA_PATCHES = [
    ("白灰窑机房-A01-上行ODF框-01", "1-1", "c2-1-1", "轧钢监控/SCADA 1芯"),
    ("白灰窑机房-A01-上行ODF框-01", "2-2", "c2-2-2", "轧钢主控 2芯"),
    ("白灰窑机房-A01-上行ODF框-02", "1-1", "c3-1-1", "至烧结01"),
    ("白灰窑机房-A01-上行ODF框-02", "2-2", "c3-2-2", "轧钢主控 2芯"),
]

CABLE_LABELS = list(CABLE_PREFIX.keys())

CIRCUIT_DESTINATIONS = [
    ("FIB-轧钢-监控", "轧钢机房-B01-上行ODF框-01", "1-1", "烧结主控机房-A01-上行ODF框-01", "1-1"),
    ("FIB-轧钢-主控", "轧钢机房-B01-上行ODF框-01", "2-2", "烧结主控机房-A01-上行ODF框-01", "2-2"),
    ("FIB-烧结-SCADA", "烧结主控机房-A01-上行ODF框-02", "1-1", "轧钢机房-B01-上行ODF框-01", "1-1"),
]

SPLICE_PLANS = [
    ("白灰窑机房-A01-上行ODF框-01", "白灰窑01-熔接方案", INTRA_PATCHES[:2]),
    ("白灰窑机房-A01-上行ODF框-02", "白灰窑02-熔接方案", INTRA_PATCHES[2:]),
]


class API:
    def __init__(self) -> None:
        self.base = CONFIG["base_url"].rstrip("/")
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Token {CONFIG['token']}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self.s.verify = CONFIG.get("verify_ssl", True)
        self._dev: dict[str, int] = {}
        self._fc: dict[str, int] = {}
        self._fp: dict[tuple[int, str], int] = {}
        self._mod: dict[int, int] = {}

    def req(self, method: str, path: str, **kw: Any) -> Any:
        r = self.s.request(method, f"{self.base}{path}", **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    def dev_id(self, name: str) -> int:
        if name not in self._dev:
            hits = self.req("GET", "/dcim/devices/", params={"name": name, "limit": 1})
            self._dev[name] = hits["results"][0]["id"]
        return self._dev[name]

    def fc_id(self, label: str) -> int:
        if label not in self._fc:
            hits = self.req("GET", "/plugins/fms/fiber-cables/", params={"limit": 50})
            for fc in hits["results"]:
                cab = fc.get("cable") or {}
                if isinstance(cab, dict) and cab.get("label") == label:
                    self._fc[label] = fc["id"]
                    break
            else:
                c = self.req("GET", "/dcim/cables/", params={"label": label, "limit": 1})["results"][0]
                fc = self.req("GET", "/plugins/fms/fiber-cables/", params={"cable_id": c["id"], "limit": 1})["results"][0]
                self._fc[label] = fc["id"]
        return self._fc[label]

    def fp_id(self, dev_name: str, fp_name: str) -> int:
        key = (self.dev_id(dev_name), fp_name)
        if key not in self._fp:
            hits = self.req("GET", "/dcim/front-ports/", params={"device_id": key[0], "name": fp_name, "limit": 1})
            if not hits["results"]:
                raise RuntimeError(f"找不到端口 {dev_name}:{fp_name}")
            self._fp[key] = hits["results"][0]["id"]
        return self._fp[key]

    def ensure_module(self, dev_name: str) -> int:
        did = self.dev_id(dev_name)
        if did in self._mod:
            return self._mod[did]
        bays = self.req("GET", "/dcim/module-bays/", params={"device_id": did, "limit": 5})
        if not bays["results"]:
            self.req("POST", "/dcim/module-bays/", json={"device": did, "name": "Tray-1", "position": "1"})
            bays = self.req("GET", "/dcim/module-bays/", params={"device_id": did, "limit": 5})
        bay_id = bays["results"][0]["id"]
        mt = self.req("GET", "/dcim/module-types/", params={"model": "Tray-12", "limit": 1})
        if not mt["results"]:
            mfg = self.req("GET", "/dcim/manufacturers/", params={"name": "Generic", "limit": 1})["results"][0]
            mt_id = self.req("POST", "/dcim/module-types/", json={"manufacturer": mfg["id"], "model": "Tray-12"})["id"]
        else:
            mt_id = mt["results"][0]["id"]
        mods = self.req("GET", "/dcim/modules/", params={"device_id": did, "limit": 5})
        if mods["results"]:
            mid = mods["results"][0]["id"]
        else:
            mid = self.req("POST", "/dcim/modules/", json={"device": did, "module_type": mt_id, "module_bay": bay_id, "status": "active"})["id"]
        self._mod[did] = mid
        return mid

    def rename_strands(self, fc_id: int, prefix: str) -> None:
        strands = self.req("GET", "/plugins/fms/fiber-strands/", params={"fiber_cable_id": fc_id, "limit": 50})
        for s in strands["results"]:
            pos = s["position"]
            new_name = f"{prefix}-{pos}-{pos}"
            if s["name"] != new_name:
                self.req("PATCH", f"/plugins/fms/fiber-strands/{s['id']}/", json={"name": new_name})

    def provision_cable(self, dev_name: str, cable_label: str) -> None:
        prefix = CABLE_PREFIX[cable_label]
        did = self.dev_id(dev_name)
        fc = self.fc_id(cable_label)
        fps = self.req("GET", "/dcim/front-ports/", params={"device_id": did, "limit": 200})
        if any(fp["name"].startswith(f"{prefix}-") for fp in fps["results"]):
            return
        self.rename_strands(fc, prefix)
        try:
            self.req("POST", "/plugins/fms/provision-ports/", json={"fiber_cable_id": fc, "device_id": did, "port_type": "splice"})
            print(f"  配置端口: {dev_name} @ {cable_label} -> {prefix}-*")
        except RuntimeError as e:
            if "duplicate" in str(e).lower():
                print(f"  跳过(已存在): {dev_name} @ {cable_label}")
            else:
                raise
        for pos in range(1, 13):
            name = f"{prefix}-{pos}-{pos}"
            hits = self.req("GET", "/dcim/front-ports/", params={"device_id": did, "name": name, "limit": 1})
            if hits["results"]:
                self._fp[(did, name)] = hits["results"][0]["id"]

    def cleanup_test_artifacts(self) -> None:
        for label in ("PATCH-01-02",):
            hits = self.req("GET", "/dcim/cables/", params={"label": label, "limit": 1})
            if hits["results"]:
                self.req("DELETE", f"/dcim/cables/{hits['results'][0]['id']}/")
                print(f"  删除测试跳线: {label}")

        dev_name = "白灰窑机房-A01-上行ODF框-01"
        did = self.dev_id(dev_name)
        for fp_name in ("link-01-02",):
            hits = self.req("GET", "/dcim/front-ports/", params={"device_id": did, "name": fp_name, "limit": 1})
            if hits["results"]:
                fp = hits["results"][0]
                if not fp.get("cable"):
                    self.req("DELETE", f"/dcim/front-ports/{fp['id']}/")
                    print(f"  删除测试端口: {fp_name}")

        for plan_name in ("测试熔接",):
            hits = self.req("GET", "/plugins/fms/splice-plans/", params={"limit": 50})
            for p in hits["results"]:
                if p["name"] == plan_name and p.get("name") != "白灰窑01-熔接方案":
                    entries = self.req("GET", "/plugins/fms/splice-plan-entries/", params={"plan_id": p["id"], "limit": 50})
                    for e in entries["results"]:
                        self.req("DELETE", f"/plugins/fms/splice-plan-entries/{e['id']}/")
                    self.req("DELETE", f"/plugins/fms/splice-plans/{p['id']}/")
                    print(f"  删除测试熔接方案: {plan_name}")

    def ports_linked(self, dev_name: str, fp_a: str, fp_b: str) -> bool:
        a = self.fp_id(dev_name, fp_a)
        b = self.fp_id(dev_name, fp_b)
        fp = self.req("GET", f"/dcim/front-ports/{a}/")
        for peer in fp.get("link_peers") or []:
            if peer.get("id") == b:
                return True
        return False

    def ensure_patch(self, dev_name: str, fp_a: str, fp_b: str) -> None:
        if self.ports_linked(dev_name, fp_a, fp_b):
            print(f"  跳线已存在: {dev_name} {fp_a} <-> {fp_b}")
            return
        label = f"PATCH-{dev_name}-{fp_a}-{fp_b}"[:100]
        label = re.sub(r"[^\w\-]", "-", label)
        hits = self.req("GET", "/dcim/cables/", params={"label": label, "limit": 1})
        if hits["results"]:
            return
        a = self.fp_id(dev_name, fp_a)
        b = self.fp_id(dev_name, fp_b)
        self.req(
            "POST",
            "/dcim/cables/",
            json={
                "type": "smf",
                "status": "connected",
                "label": label,
                "a_terminations": [{"object_type": "dcim.frontport", "object_id": a}],
                "b_terminations": [{"object_type": "dcim.frontport", "object_id": b}],
            },
        )
        print(f"  跳线: {dev_name} {fp_a} <-> {fp_b}")

    def assign_ports_to_tray(self, dev_name: str, fp_names: list[str]) -> None:
        tray = self.ensure_module(dev_name)
        for name in fp_names:
            fp_id = self.fp_id(dev_name, name)
            fp = self.req("GET", f"/dcim/front-ports/{fp_id}/")
            mod = fp.get("module")
            mod_id = mod["id"] if isinstance(mod, dict) else mod
            if mod_id != tray:
                self.req("PATCH", f"/dcim/front-ports/{fp_id}/", json={"module": tray})

    def ensure_splice_plan(self, dev_name: str, plan_name: str, patches: list[tuple]) -> None:
        fp_names = sorted({a for _d, a, b, _n in patches} | {b for _d, a, b, _n in patches})
        self.assign_ports_to_tray(dev_name, fp_names)

        did = self.dev_id(dev_name)
        hits = self.req("GET", "/plugins/fms/splice-plans/", params={"closure_id": did, "limit": 10})
        plan = None
        for p in hits["results"]:
            if p["name"] == plan_name:
                plan = p
                break
        if not plan:
            plan = self.req("POST", "/plugins/fms/splice-plans/", json={"closure": did, "name": plan_name, "description": "测试熔接"})
            print(f"  熔接方案: {plan_name}")
        else:
            plan_id = plan["id"]
            plan = self.req("PATCH", f"/plugins/fms/splice-plans/{plan_id}/", json={"status": "active"})

        plan_id = plan["id"]
        tray = self.ensure_module(dev_name)
        existing = self.req("GET", "/plugins/fms/splice-plan-entries/", params={"plan_id": plan_id, "limit": 50})

        def _port_id(v: Any) -> int:
            return v["id"] if isinstance(v, dict) else int(v)

        existing_pairs = {(_port_id(e["fiber_a"]), _port_id(e["fiber_b"])) for e in existing["results"]}
        for _dev, fp_a, fp_b, note in patches:
            fa = self.fp_id(dev_name, fp_a)
            fb = self.fp_id(dev_name, fp_b)
            if (fa, fb) in existing_pairs or (fb, fa) in existing_pairs:
                continue
            self.req(
                "POST",
                "/plugins/fms/splice-plan-entries/",
                json={"plan": plan_id, "tray": tray, "fiber_a": fa, "fiber_b": fb, "notes": note},
            )
            print(f"  熔接条目: {fp_a} <-> {fp_b} ({note})")
        self.req("PATCH", f"/plugins/fms/splice-plans/{plan_id}/", json={"status": "active"})

    def rp_id(self, dev_name: str, rp_name: str) -> int:
        did = self.dev_id(dev_name)
        hits = self.req("GET", "/dcim/rear-ports/", params={"device_id": did, "name": rp_name, "limit": 1})
        if not hits["results"]:
            raise RuntimeError(f"找不到后端口 {dev_name}:{rp_name}")
        return hits["results"][0]["id"]

    def fix_cable_terminations(self) -> None:
        for label in CABLE_LABELS:
            hits = self.req("GET", "/dcim/cables/", params={"label": label, "limit": 1})
            if not hits["results"]:
                continue
            cable = hits["results"][0]
            a_obj = cable["a_terminations"][0]["object"]
            b_obj = cable["b_terminations"][0]["object"]
            dev_a = a_obj["device"]["name"]
            dev_b = b_obj["device"]["name"]
            rp_a = self.rp_id(dev_a, label)
            rp_b = self.rp_id(dev_b, label)
            if a_obj["id"] == rp_a and b_obj["id"] == rp_b:
                continue
            self.req(
                "PATCH",
                f"/dcim/cables/{cable['id']}/",
                json={
                    "a_terminations": [{"object_type": "dcim.rearport", "object_id": rp_a}],
                    "b_terminations": [{"object_type": "dcim.rearport", "object_id": rp_b}],
                },
            )
            print(f"  缆线改接: {label} -> FMS后端口")

    def fix_path_destinations(self) -> None:
        for cid, _dev_a, port_a, dev_b, port_b in CIRCUIT_DESTINATIONS:
            circ = self.req("GET", "/plugins/fms/fiber-circuits/", params={"cid": cid, "limit": 1})
            if not circ["results"]:
                continue
            circuit_id = circ["results"][0]["id"]
            origin_id = self.fp_id(_dev_a, port_a)
            dest_id = self.fp_id(dev_b, port_b)
            paths = self.req("GET", "/plugins/fms/fiber-circuit-paths/", params={"circuit_id": circuit_id, "limit": 5})
            for p in paths["results"]:
                cur_origin = p.get("origin")
                cur_dest = p.get("destination")
                if isinstance(cur_origin, dict):
                    cur_origin = cur_origin.get("id")
                if isinstance(cur_dest, dict):
                    cur_dest = cur_dest.get("id")
                if cur_origin != origin_id or cur_dest != dest_id:
                    self.req(
                        "PATCH",
                        f"/plugins/fms/fiber-circuit-paths/{p['id']}/",
                        json={"origin": origin_id, "destination": dest_id},
                    )
                    print(f"  光路端点: {cid} -> {dev_b}:{port_b}")

    def rename_primary_ports(self, dev_name: str) -> None:
        cables = DEVICE_CABLES.get(dev_name, [])
        c1_labels = [cab for cab in cables if CABLE_PREFIX.get(cab) == "c1"]
        if not c1_labels:
            return
        label = c1_labels[0]
        did = self.dev_id(dev_name)
        fps = self.req("GET", "/dcim/front-ports/", params={"device_id": did, "limit": 200})["results"]
        for fp in fps:
            name = fp["name"]
            new_name: str | None = None
            if name.startswith("c1-"):
                parts = name.split("-")
                if len(parts) == 3 and parts[1] == parts[2]:
                    new_name = f"{parts[1]}-{parts[2]}"
            elif name.startswith(f"{label}:F"):
                pos = name.rsplit("F", 1)[-1]
                if pos.isdigit():
                    new_name = f"{pos}-{pos}"
            if not new_name or name == new_name:
                continue
            clash = self.req("GET", "/dcim/front-ports/", params={"device_id": did, "name": new_name, "limit": 1})
            if clash["results"] and clash["results"][0]["id"] != fp["id"]:
                continue
            self.req("PATCH", f"/dcim/front-ports/{fp['id']}/", json={"name": new_name})
            self._fp[(did, new_name)] = fp["id"]
            print(f"  重命名: {dev_name} {name} -> {new_name}")

    def rename_secondary_ports(self, dev_name: str) -> None:
        did = self.dev_id(dev_name)
        fps = self.req("GET", "/dcim/front-ports/", params={"device_id": did, "limit": 200})["results"]
        for cab in DEVICE_CABLES.get(dev_name, []):
            prefix = CABLE_PREFIX[cab]
            if prefix == "c1":
                continue
            for fp in fps:
                name = fp["name"]
                new_name: str | None = None
                if name.startswith(f"{cab}:F"):
                    pos = name.rsplit("F", 1)[-1]
                    if pos.isdigit():
                        new_name = f"{prefix}-{pos}-{pos}"
                if not new_name or name == new_name:
                    continue
                clash = self.req("GET", "/dcim/front-ports/", params={"device_id": did, "name": new_name, "limit": 1})
                if clash["results"] and clash["results"][0]["id"] != fp["id"]:
                    continue
                self.req("PATCH", f"/dcim/front-ports/{fp['id']}/", json={"name": new_name})
                self._fp[(did, new_name)] = fp["id"]
                print(f"  重命名: {dev_name} {name} -> {new_name}")

    def retrace_circuits(self) -> None:
        circuits = self.req("GET", "/plugins/fms/fiber-circuits/", params={"limit": 20})
        for circ in circuits["results"]:
            self.req("POST", f"/plugins/fms/fiber-circuits/{circ['id']}/retrace/")
            paths = self.req("GET", "/plugins/fms/fiber-circuit-paths/", params={"circuit_id": circ["id"], "limit": 5})
            for p in paths["results"]:
                complete = "完整" if p["is_complete"] else "未完整"
                print(f"  重算 {circ['cid']}: {complete}, hops={len(p.get('path') or [])}")


def main() -> None:
    api = API()
    print("==> 清理测试残留")
    api.cleanup_test_artifacts()

    print("==> 多缆线端口（唯一纤芯名）")
    for dev_name, cables in DEVICE_CABLES.items():
        for cab in cables:
            api.provision_cable(dev_name, cab)

    print("==> 缆线改接到 FMS 后端口")
    api.fix_cable_terminations()

    print("==> 首缆端口重命名为 1-1 格式")
    for dev_name in DEVICE_CABLES:
        api.rename_primary_ports(dev_name)

    print("==> 次缆端口重命名为 c2-1-1 格式")
    for dev_name in DEVICE_CABLES:
        api.rename_secondary_ports(dev_name)

    print("==> 跳线 + 熔接方案")
    for dev_name, plan_name, patches in SPLICE_PLANS:
        for _d, a, b, _n in patches:
            api.ensure_patch(dev_name, a, b)
        api.ensure_splice_plan(dev_name, plan_name, patches)

    print("==> 补全光路端点")
    api.fix_path_destinations()

    print("==> 重算光路")
    api.retrace_circuits()
    print("\n熔接方案补全完成。")


if __name__ == "__main__":
    main()
