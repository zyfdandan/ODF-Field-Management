"""Canonical ODF device names for mock data and rename migration."""

from __future__ import annotations

# 机房至机房命名：本端机房至对端机房
DEV_ZG = "轧钢机房至白灰窑机房"
DEV_BHY1 = "白灰窑机房至轧钢机房"
DEV_BHY2 = "白灰窑机房至烧结主控机房"
DEV_SJ1 = "烧结主控机房至白灰窑机房"

DEVICE_RENAME_MAP: dict[str, str] = {
    "轧钢机房-JG01-ODF-01": DEV_ZG,
    "白灰窑机房-A01-ODF-01": DEV_BHY1,
    "白灰窑机房-A01-ODF-02": DEV_BHY2,
    "烧结主控机房-A01-ODF-01": DEV_SJ1,
    "轧钢机房至白灰窑机房": DEV_ZG,
    "白灰窑机房至轧钢机房": DEV_BHY1,
}

DEVICE_RENAME_MAP = {k: v for k, v in DEVICE_RENAME_MAP.items() if v}
