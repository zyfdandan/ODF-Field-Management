#!/usr/bin/env python3
"""Unit tests for P1 swap-cascade behavior (no NetBox)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "field_portal")]

from field_service import _apply_jump_swap_cascade, _simulated_order_after_jump_swap  # noqa: E402


def _row(da, pa, db, pb, cable="", jump=False):
    label = f"{da} {pa} -> {db} {pb}"
    if jump:
        label += " (跳纤段)"
    return {
        "dev_a": da,
        "port_a": pa,
        "dev_b": db,
        "port_b": pb,
        "cable": cable,
        "cab_a": cable,
        "cab_b": cable,
        "label_pos": label,
    }


def test_cascade_updates_jump_after_unrelated_gap():
    ordered = [
        _row("地下室", "1-6", "主控地下", "1-6", "地下室缆"),
        _row("无关A", "9-1", "无关B", "9-1", "无关缆"),
        _row("主控地下", "1-6", "主控轧钢", "2-5", "轧钢缆", jump=True),
        _row("主控轧钢", "2-5", "轧钢", "2-5", "轧钢缆"),
    ]
    _apply_jump_swap_cascade(ordered, 0, "1-6", "1-6", "1-6", "1-7")
    assert ordered[2]["port_a"] == "1-7", ordered[2]
    assert ordered[2]["port_b"] == "2-5", ordered[2]
    assert ordered[3]["port_a"] == "2-5"
    assert ordered[3]["port_b"] == "2-5"
    assert "跳纤段" in ordered[2]["label_pos"]
    assert "1-7" in ordered[2]["label_pos"]


def test_simulated_trunk_swap_includes_later_jump():
    ordered = [
        _row("地下室", "1-6", "主控地下", "1-6", "地下室缆"),
        _row("无关A", "9-1", "无关B", "9-1", "无关缆"),
        _row("主控地下", "1-6", "主控轧钢", "2-5", "轧钢缆", jump=True),
    ]
    sim = _simulated_order_after_jump_swap(ordered, 0, "1-6", "1-7")
    assert sim[0]["port_b"] == "1-7"
    assert sim[2]["port_a"] == "1-7"
    assert sim[2]["port_b"] == "2-5"
    assert ordered[2]["port_a"] == "1-6"


if __name__ == "__main__":
    test_cascade_updates_jump_after_unrelated_gap()
    test_simulated_trunk_swap_includes_later_jump()
    print("ok")
