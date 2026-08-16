#!/usr/bin/env python3
"""Generate 现场登记.xlsx for portal whitelist test routes (排-芯 naming)."""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from naming_rules import site_slug
from site_devices import DEV_BHY1, DEV_SJ1, DEV_ZG
from unified_sheet import (
    BASE_HEADERS,
    BUSINESS_HEADERS,
    LOSS_HEADERS,
    SHEET_BASE,
    SHEET_BUSINESS,
    SHEET_LOSS,
)

OUT = Path(__file__).with_name("现场登记_new.xlsx")

BASE_ROWS = [
    ["站点", "轧钢厂", site_slug("轧钢厂"), "", ""],
    ["站点", "烧结厂", site_slug("烧结厂"), "", ""],
    ["ODF", DEV_ZG, "轧钢厂", "轧钢机房", "JG1"],
    ["ODF", DEV_BHY1, "烧结厂", "白灰窑机房", "JG1"],
    ["ODF", DEV_SJ1, "烧结厂", "烧结主控机房", "A01"],
    ["缆段", "CBL-轧钢-白灰窑", DEV_ZG, DEV_BHY1, "G.652D-12 / 12芯"],
    ["缆段", "CBL-白灰窑-烧结", DEV_BHY1, DEV_SJ1, "G.652D-12 / 12芯"],
]


def _biz(
    cid: str,
    cable: str,
    odf_a: str,
    odf_b: str,
    port_a: str,
    port_b: str | None = None,
    *,
    site: str = "",
    location: str = "",
    name: str = "",
    service: str = "",
    ip: str = "",
    label: str = "",
    note: str = "",
) -> list:
    if port_b is None:
        port_b = port_a
    return [
        site,
        location,
        cable,
        cid,
        odf_a,
        odf_b,
        name,
        port_a,
        port_b,
        service,
        ip,
        label or f"{odf_a} {port_a} -> {odf_b} {port_b}",
        note,
        "",
        "",
    ]


# Portal test circuit: 轧钢 -> 白灰窑 -> 烧结 (纤芯 1-1 贯通)
BUSINESS_ROWS = [
    _biz(
        "FIB-烧结主控办公网",
        "CBL-轧钢-白灰窑",
        DEV_ZG,
        DEV_BHY1,
        "1-1",
        "1-1",
        site="轧钢厂",
        location="轧钢机房",
        name="烧结主控办公网",
        service="办公网",
        ip="10.1.3.1",
        label="轧钢至白灰窑 1-1",
    ),
    _biz(
        "FIB-烧结主控办公网",
        "CBL-白灰窑-烧结",
        DEV_BHY1,
        DEV_SJ1,
        "1-1",
        "1-1",
        site="烧结厂",
        location="白灰窑机房",
        label="白灰窑至烧结主控 1-1",
    ),
]

LOSS_ROWS = [
    ["", DEV_ZG, "1-1", "CBL-轧钢-白灰窑", "0.35", "1310", "轧钢侧"],
    ["", DEV_BHY1, "1-1", "CBL-轧钢-白灰窑", "0.38", "1310", "白灰窑接轧钢缆"],
    ["", DEV_BHY1, "1-1", "CBL-白灰窑-烧结", "0.40", "1310", "白灰窑至烧结缆"],
    ["", DEV_SJ1, "1-1", "CBL-白灰窑-烧结", "0.36", "1310", "烧结侧"],
    ["FIB-烧结主控办公网", "", "", "", "1.49", "1310", "段合计"],
]


def main() -> None:
    wb = Workbook()
    ws0 = wb.active
    ws0.title = SHEET_BASE
    ws0.append(BASE_HEADERS)
    for row in BASE_ROWS:
        ws0.append(row)

    ws1 = wb.create_sheet(SHEET_LOSS)
    ws1.append(LOSS_HEADERS)
    for row in LOSS_ROWS:
        ws1.append(row)

    ws2 = wb.create_sheet(SHEET_BUSINESS)
    ws2.append(BUSINESS_HEADERS)
    for row in BUSINESS_ROWS:
        ws2.append(row)

    wb.save(OUT)
    print(f"已写入 {OUT}（光路 1 条 / 段 2 / ODF 3 / 缆段 2）")


if __name__ == "__main__":
    main()
