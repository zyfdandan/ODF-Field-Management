#!/usr/bin/env python3
"""Reset field portal test lab: clear pending store, optional FMS circuits."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from field_service import client_from_config, load_config, paginate

STORE = Path(__file__).with_name("circuit_store.json")
KEEP_CIDS: set[str] = set()


def clear_store() -> None:
    STORE.write_text("{}\n", encoding="utf-8")
    print(f"Cleared {STORE}")


def delete_fms_circuits(client, *, dry_run: bool) -> None:
    removed = 0
    for circ in paginate(client, "/plugins/fms/fiber-circuits/"):
        cid = (circ.get("cid") or circ.get("name") or "").strip()
        if cid in KEEP_CIDS:
            continue
        if dry_run:
            print(f"  would delete circuit {cid} (id={circ['id']})")
        else:
            client.request("DELETE", f"/plugins/fms/fiber-circuits/{circ['id']}/")
            print(f"  deleted circuit {cid}")
        removed += 1
    print(f"FMS circuits affected: {removed}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset ODF field portal test data")
    parser.add_argument("--delete-fms", action="store_true", help="Delete all FMS fiber circuits")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    clear_store()
    if args.delete_fms:
        client = client_from_config(load_config())
        delete_fms_circuits(client, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
