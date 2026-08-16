#!/usr/bin/env python3
"""Clear FMS/OSP import test data and PATCH/LINK jump cables before re-import."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from import_from_excel import NetBoxClient

DEFAULT_CONFIG = Path(__file__).with_name("netbox_config.json")


def paginate(client: NetBoxClient, path: str, params: dict | None = None) -> list[dict]:
    params = dict(params or {})
    params.setdefault("limit", 200)
    out: list[dict] = []
    while True:
        data = client.request("GET", path, params=params)
        out.extend(data.get("results", []))
        nxt = data.get("next")
        if not nxt:
            break
        params = {"limit": params["limit"], "offset": params.get("offset", 0) + params["limit"]}
    return out


def delete_all(client: NetBoxClient, label: str, path: str, params: dict | None = None) -> int:
    try:
        items = paginate(client, path, params)
    except RuntimeError as e:
        if "404" in str(e):
            return 0
        raise
    for item in items:
        client.request("DELETE", f"{path.rstrip('/')}/{item['id']}/")
    if items:
        print(f"  删除 {len(items)} 条 {label}")
    return len(items)


def delete_osp_data(client: NetBoxClient) -> None:
    """Remove OSP splices/strands/tubes/trays/closures/cables (dependency order)."""
    for label, path in (
        ("OSP 熔接点", "/plugins/osp/splices/"),
        ("OSP 光纤链路", "/plugins/osp/links/"),
        ("OSP 纤芯", "/plugins/osp/strands/"),
        ("OSP 束管", "/plugins/osp/tubes/"),
        ("OSP 熔接盘", "/plugins/osp/trays/"),
        ("OSP 接头盒", "/plugins/osp/closures/"),
        ("OSP 光缆", "/plugins/osp/cables/"),
    ):
        delete_all(client, label, path)


def count_osp(client: NetBoxClient) -> dict[str, int]:
    counts = {}
    for key, path in (
        ("splices", "/plugins/osp/splices/"),
        ("links", "/plugins/osp/links/"),
        ("strands", "/plugins/osp/strands/"),
        ("tubes", "/plugins/osp/tubes/"),
        ("trays", "/plugins/osp/trays/"),
        ("closures", "/plugins/osp/closures/"),
        ("cables", "/plugins/osp/cables/"),
    ):
        counts[key] = len(paginate(client, path))
    return counts


def delete_jump_cables(client: NetBoxClient) -> int:
    cables = paginate(client, "/dcim/cables/")
    removed = 0
    for cab in cables:
        label = cab.get("label") or ""
        if label.startswith("PATCH-") or label.startswith("LINK-"):
            try:
                client.request("DELETE", f"/dcim/cables/{cab['id']}/")
                removed += 1
            except RuntimeError as e:
                if "409" in str(e):
                    print(f"  跳过跳线 {label} (仍有 FMS 节点引用，需 purge_fms_nodes)")
                else:
                    raise
    if removed:
        print(f"  删除 {removed} 条 PATCH/LINK 跳线")
    return removed


def purge_fms_nodes_via_docker() -> bool:
    """Delete orphan FiberCircuitNode rows blocking DCIM cable delete (NetBox Docker)."""
    import subprocess

    script = (
        "from netbox_fms.models import FiberCircuitNode\n"
        "n = FiberCircuitNode.objects.count()\n"
        "FiberCircuitNode.objects.all().delete()\n"
        "print(f'deleted {n} FiberCircuitNode')\n"
    )
    hostkey = os.environ.get("NETBOX_SSH_HOSTKEY", "").strip()
    password = os.environ.get("NETBOX_SSH_PASSWORD", "").strip()
    ssh_target = os.environ.get("NETBOX_SSH_TARGET", "").strip()  # e.g. user@host
    if not password or not ssh_target:
        print("  WARN: 跳过 FMS purge（请设置环境变量 NETBOX_SSH_TARGET / NETBOX_SSH_PASSWORD）")
        return False
    cmd = [
        "plink",
        "-batch",
        "-pw",
        password,
        ssh_target,
        "docker exec -i netbox-docker-netbox-1 python /opt/netbox/netbox/manage.py shell",
    ]
    if hostkey:
        cmd[2:2] = ["-hostkey", hostkey]
    try:
        proc = subprocess.run(cmd, input=script.encode("utf-8"), capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"  WARN: 无法 purge FMS nodes ({e})")
        return False
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    if proc.returncode == 0:
        for line in out.splitlines():
            if "deleted" in line:
                print(f"  {line.strip()}")
        return True
    print(f"  WARN: purge FMS nodes failed: {err[:200] or out[:200]}")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset FMS/OSP import test data (keep ODF + trunk CBL cables)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-osp", action="store_true", help="仅清除 FMS，保留 OSP 数据")
    args = parser.parse_args()

    if not args.config.exists():
        print(f"缺少配置: {args.config}", file=sys.stderr)
        sys.exit(1)

    config = json.loads(args.config.read_text(encoding="utf-8"))
    client = NetBoxClient(config["base_url"], config["token"], config.get("verify_ssl", True))

    print("==> 清除 FMS/OSP 测试数据及 PATCH-LINK 跳线")
    if args.dry_run:
        paths = paginate(client, "/plugins/fms/fiber-circuit-paths/")
        circuits = paginate(client, "/plugins/fms/fiber-circuits/")
        entries = paginate(client, "/plugins/fms/splice-plan-entries/")
        plans = paginate(client, "/plugins/fms/splice-plans/")
        jumps = [
            c for c in paginate(client, "/dcim/cables/") if (c.get("label") or "").startswith(("PATCH-", "LINK-"))
        ]
        osp = count_osp(client) if not args.skip_osp else {}
        print(f"  DRY-RUN: 路径 {len(paths)}, 光路 {len(circuits)}, 熔接条目 {len(entries)}, "
              f"熔接方案 {len(plans)}, 跳线 {len(jumps)}")
        if osp:
            print(f"  DRY-RUN OSP: 熔接 {osp['splices']}, 纤芯 {osp['strands']}, 光缆 {osp['cables']}, "
                  f"接头盒 {osp['closures']}, 托盘 {osp['trays']}")
        return

    delete_all(client, "光路路径", "/plugins/fms/fiber-circuit-paths/")
    delete_all(client, "光路", "/plugins/fms/fiber-circuits/")
    delete_all(client, "熔接条目", "/plugins/fms/splice-plan-entries/")
    delete_all(client, "熔接方案", "/plugins/fms/splice-plans/")
    if not args.skip_osp:
        delete_osp_data(client)
    purge_fms_nodes_via_docker()
    delete_jump_cables(client)
    print("完成。可运行 generate_unified.py + import_from_excel.py --link-mode port 重新导入。")


if __name__ == "__main__":
    main()
