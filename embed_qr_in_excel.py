#!/usr/bin/env python3
"""Embed existing QR PNG files into the Excel 光路录入 sheet."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from excel_qr import embed_from_manifest

DEFAULT_EXCEL = Path(__file__).with_name("现场光路录入.xlsx")
DEFAULT_MANIFEST = Path(__file__).with_name("qr_labels") / "qr_manifest.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed QR images into Excel from qr_manifest.json")
    parser.add_argument("--excel", type=Path, default=DEFAULT_EXCEL)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    if not args.excel.exists():
        print(f"缺少 Excel: {args.excel}", file=sys.stderr)
        sys.exit(1)
    if not args.manifest.exists():
        print(f"缺少 manifest: {args.manifest}（请先运行 import_from_excel.py）", file=sys.stderr)
        sys.exit(1)
    n = embed_from_manifest(args.excel, args.manifest)
    print(f"已嵌入 {n} 个二维码到 {args.excel}")


if __name__ == "__main__":
    main()
