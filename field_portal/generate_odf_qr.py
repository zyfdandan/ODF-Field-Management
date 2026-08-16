#!/usr/bin/env python3
"""Generate QR codes for each ODF — scan opens Node-RED field form."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from excel_qr import save_qr_png

DEFAULT_CONFIG = Path(__file__).with_name("portal_config.json")
OUT_DIR = Path(__file__).resolve().parent.parent / "qr_labels" / "odf_field"


def load_portal_config() -> dict:
    if DEFAULT_CONFIG.exists():
        return json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    return {
        "nodered_base_url": "http://YOUR_NODERED_HOST:1880",
        "form_path": "/odf",
    }


def form_url(base: str, form_path: str, odf: str) -> str:
    base = base.rstrip("/")
    path = form_path if form_path.startswith("/") else f"/{form_path}"
    from urllib.parse import quote

    return f"{base}{path}?odf={quote(odf)}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate ODF field QR codes")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--excel", type=Path, default=Path(__file__).resolve().parent.parent / "现场登记_new.xlsx")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--base-url", default="", help="Override Node-RED base URL")
    args = parser.parse_args()

    cfg = load_portal_config()
    if args.config.exists():
        cfg.update(json.loads(args.config.read_text(encoding="utf-8")))
    base = args.base_url or cfg.get("nodered_base_url", "http://127.0.0.1:1880")
    form_path = cfg.get("form_path", "/odf")

    odf_names: list[str] = []
    if args.excel.exists():
        import openpyxl
        from unified_sheet import parse_base_sheet, parse_workbook

        wb = openpyxl.load_workbook(args.excel)
        data = parse_workbook(wb)
        odf_names = sorted({d["name"] for d in data.get("devices", []) if d.get("name")})
        if not odf_names:
            base_data = parse_base_sheet(wb)
            odf_names = sorted({d["name"] for d in base_data.get("devices", []) if d.get("name")})

    if not odf_names:
        odf_names = [
            "轧钢机房-JG1-ODF-01",
            "白灰窑机房-JG1-ODF-01",
            "白灰窑机房-JG2-ODF-01",
            "烧结主控机房-A01-ODF-01",
        ]

    args.out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    for name in odf_names:
        url = form_url(base, form_path, name)
        safe = name.replace("/", "_").replace("\\", "_")
        png = args.out / f"{safe}.png"
        if save_qr_png(png, url):
            manifest.append({"odf": name, "url": url, "qr_file": str(png.relative_to(args.out.parent.parent))})
            print(f"  {name} -> {url}")
        else:
            print(f"  跳过 {name}（需 pip install qrcode[pil]）")

    (args.out / "odf_field_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已生成 {len(manifest)} 个 ODF 二维码 -> {args.out}")


if __name__ == "__main__":
    main()
