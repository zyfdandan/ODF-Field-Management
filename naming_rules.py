"""Naming conventions for field registration and NetBox import."""

from __future__ import annotations

import re

# (对象, 格式, 示例, 说明)
NAMING_RULES: list[tuple[str, str, str, str]] = [
    (
        "厂区站点",
        "{厂区名}",
        "得丰焦化",
        "NetBox 站点名称；全厂可共用一个站点",
    ),
    (
        "机房",
        "{区域/工序}机房",
        "轧钢机房、白灰窑机房、烧结主控机房",
        "现场登记「机房/位置」列",
    ),
    (
        "ODF 框（设备全名）",
        "{本端机房}至{对端机房}[用途后缀]",
        "得丰机房至轧钢机房、得丰机房至轧钢机房备用",
        "本端机房安装；同路由多条缆时加 备用/生产/监控",
    ),
    (
        "缆段编号",
        "{起点机房}至{终点机房}[用途后缀]",
        "得丰机房至轧钢机房、白灰窑机房至烧结主控机房监控",
        "与光纤走向一致；同机房对多条缆用 备用/生产/监控 区分",
    ),
    (
        "光路编号",
        "FIB-{区域/工序}-{业务}",
        "FIB-轧钢-监控、FIB-烧结-SCADA",
        "相同编号=一条业务链路；业务表每段重复同一编号",
    ),
    (
        "光路名称",
        "{业务描述}",
        "轧钢线监控、烧结SCADA采集",
        "首段填写即可，后续段可留空",
    ),
    (
        "纤芯（录入）",
        "{排}-{芯}",
        "1-1、1-12、2-1（24芯第二排）",
        "第1排 1-1~1-12；24芯ODF 第2排 2-1~2-12",
    ),
    (
        "前置端口（系统生成）",
        "{本端缩写}to{对端缩写}{排}-{芯}",
        "DFtoXZJ1-1、ZGtoBHY2-3",
        "由缆段+本端机房+纤芯自动生成；每排12芯 1-1~1-12、2-1~2-12",
    ),
    (
        "标签位置（现场）",
        "{ODF全名} {前置端口名}",
        "白灰窑机房至轧钢机房 白灰窑至轧钢1-1",
        "业务表「标签位置」；建议贴在业务终点 ODF 或熔接点",
    ),
    (
        "ODF 框标签（导入后）",
        "{ODF}\\nODF\\n对端：{对端ODF}\\n全程：{路由}\\n{光路编号}",
        "见 qr_labels/odf_labels.json",
        "导入后自动生成建议内容，可打印贴框",
    ),
    (
        "业务二维码标签",
        "{光路编号} → NetBox 追踪页",
        "FIB-轧钢-监控",
        "导入后写入「扫码链接/二维码」列",
    ),
    (
        "光损录入（现场）",
        "{ODF框全名} {纤芯(排-芯)}",
        "轧钢机房至白灰窑机房 1-1",
        "光损录入表按 ODF 框标记；交汇 ODF 需同时填缆段编号区分",
    ),
    (
        "同机房跳纤（系统）",
        "PATCH-{ODF}-{端口A}-{端口B}",
        "PATCH-白灰窑…-1-1-…",
        "同 ODF 内 FrontPort 连接，导入自动创建",
    ),
    (
        "跨框跳纤（系统）",
        "LINK-{ODFA}-{端口A}-{ODFB}-{端口B}",
        "LINK-白灰窑…-烧结…",
        "跨 ODF 连接，相邻光路段换缆时自动创建",
    ),
]

GUIDE_NAMING_LINES = [
    "",
    "── 命名规范（摘要）──",
    "  机房：{区域}机房，如 轧钢机房、白灰窑机房",
    "  ODF框：{本端机房}至{对端机房}，如 得丰机房至轧钢机房",
    "  同路由多条：加 备用 / 生产 / 监控，如 得丰机房至轧钢机房备用",
    "  缆段：与 ODF 同格式，如 白灰窑机房至烧结主控机房",
    "  端口：{本端缩写}to{对端缩写}{排}-{芯}，如 DFtoXZJ1-3、ZGtoBHY1-12",
    "  光路：FIB-{区域}-{业务}，如 FIB-轧钢-监控",
    "  详见工作表「命名规范」",
]

SITE_SLUGS = {
    "得丰焦化": "defeng-coking",
    "钢铁园区": "steel-park",
}

ROUTE_SUFFIXES: tuple[str, ...] = ("备用", "生产", "监控", "专网", "SCADA")

# 站点/机房缩写（端口名 DFtoXZJ）；未列出的机房由 room_abbrev() 自动分配
ENDPOINT_ABBREVS: dict[str, str] = {
    "轧钢": "ZG",
    "白灰窑": "BHY",
    "白灰窑2": "BHY2",
    "烧结": "SJ",
    "烧结主控": "SJ",
    "得丰": "DF",
    "炼钢主控": "LG",
    "新质检": "XZJ",
    "中唐大库": "ZTD",
    "新质检中心": "XZJ",
    "空压站": "KYZ",
    "炼钢主控楼": "LG",
    "炼铁1#炉主控楼": "LT1",
    "炼铁2#炉主控楼": "LT2",
    "烧结白灰主控楼": "BHY",
    "焦化二期10KV变电站": "JH10KV",
    "加热炉": "JRL",
    "成品库": "CPK",
    "喷煤": "PM",
    "风机房": "FJ",
    "炼铁轨道衡": "LTGDH",
    "10万伏主控室": "WF10",
    "二期3、4号磅房": "BF34",
    # 数字开头机房：禁止只取 "900"（会互撞且与纤芯粘连成 900to9001-6）
    "900线精轧": "900JR",
    "900线精轧机房": "900JR",
    "900线主控楼": "900ZK",
    "900线卷曲": "900JQ",
    "900线粗轧": "900CZ",
    "900线质检站": "900ZJ",
}


CHAR_ABBR: dict[str, str] = {
    "得": "D",
    "丰": "F",
    "炼": "L",
    "钢": "G",
    "铁": "T",
    "空": "K",
    "压": "Y",
    "站": "Z",
    "烧": "S",
    "结": "J",
    "白": "B",
    "灰": "H",
    "窑": "Y",
    "主": "Z",
    "控": "K",
    "楼": "L",
    "室": "S",
    "新": "X",
    "质": "Z",
    "检": "J",
    "中": "Z",
    "唐": "T",
    "大": "D",
    "库": "K",
    "焦": "J",
    "化": "H",
    "变": "B",
    "电": "D",
    "门": "M",
    "岗": "G",
    "保": "B",
    "卫": "W",
    "制": "Z",
    "卡": "K",
    "氧": "Y",
    "喷": "P",
    "煤": "M",
    "卷": "J",
    "曲": "Q",
    "轧": "Z",
    "精": "J",
    "粗": "C",
    "成": "C",
    "品": "P",
    "园": "Y",
    "区": "Q",
    "办": "B",
    "机": "J",
    "修": "X",
    "车": "C",
    "间": "J",
    "废": "F",
    "钢": "G",
    "收": "S",
    "料": "L",
    "旋": "X",
    "流": "L",
    "井": "J",
    "炉": "L",
    "热": "R",
    "余": "Y",
    "发": "F",
    "电": "D",
    "合": "H",
    "金": "J",
    "切": "Q",
    "割": "G",
    "加": "J",
    "动": "D",
    "力": "L",
    "公": "G",
    "辅": "F",
    "平": "P",
    "房": "F",
    "德": "D",
    "川": "C",
    "办": "B",
    "公": "G",
    "磅": "B",
    "房": "F",
    "水": "S",
    "处": "C",
    "理": "L",
    "二": "E",
    "期": "Q",
    "万": "W",
    "伏": "F",
    "号": "H",
}


def _latin_abbrev_from_chinese(name: str) -> str:
    raw = asset_room_name(name)
    body = re.sub(r"(主控楼|主控室|变电站|机房|室|楼|站)$", "", raw)
    out: list[str] = []
    for ch in body:
        if ch.isdigit():
            out.append(ch)
        elif ch in CHAR_ABBR:
            out.append(CHAR_ABBR[ch])
        elif ch in "#KVkv":
            out.append(ch.upper())
    code = "".join(out).upper()
    return code[:8] if code else ""


def asset_room_name(name: str) -> str:
    """资产表机房名：保留原表写法，仅去空白。"""
    return (name or "").strip()


def normalize_room_name(name: str) -> str:
    """机房/位置名称规范化（业务登记用，可补「机房」后缀）。"""
    name = asset_room_name(name)
    if not name:
        return ""
    aliases = {
        "烧结白灰主控楼": "白灰窑机房",
        "烧结白灰主控": "白灰窑机房",
        "白灰窑": "白灰窑机房",
        "轧钢": "轧钢机房",
        "烧结主控": "烧结主控机房",
        "烧结主控楼": "烧结主控机房",
        "炼钢主控": "炼钢主控机房",
        "炼钢主控楼": "炼钢主控机房",
        "得丰": "得丰机房",
    }
    if name in aliases:
        return aliases[name]
    for k, v in aliases.items():
        if name == k or (k in name and len(name) <= len(k) + 2):
            return v
    if not name.endswith(("机房", "楼", "室", "站", "变电站")):
        if "主控" in name:
            return f"{name}机房" if "楼" not in name else name
        if len(name) <= 8 and "机房" not in name:
            return f"{name}机房"
    return name


def _room_lookup_keys(name: str) -> list[str]:
    raw = asset_room_name(name)
    if not raw:
        return []
    keys = [raw]
    stripped = re.sub(r"(机房|主控楼|主控室|变电站|室|楼|站)$", "", raw).strip()
    if stripped and stripped not in keys:
        keys.append(stripped)
    short = room_route_short(raw)
    if short and short not in keys:
        keys.append(short)
    return keys


def room_abbrev(name: str, *, registry: dict[str, str] | None = None) -> str:
    """机房缩写，用于端口名 {A}to{B}1-1。

    纯数字 ascii（如「900线精轧」→900）不可直接当缩写：多机房会撞名，
    且缩写以数字结尾时与纤芯粘连（900to9001-6 / PMtoLT11-1）。
    """
    for key in _room_lookup_keys(name):
        if key in ENDPOINT_ABBREVS:
            return ENDPOINT_ABBREVS[key]
        if registry and key in registry:
            return registry[key]
    raw = asset_room_name(name)
    ascii_part = re.sub(r"[^a-zA-Z0-9#]", "", raw).upper()
    # 须含字母或 #，禁止仅数字（否则 900线* 全变成 900）
    if len(ascii_part) >= 2 and re.search(r"[A-Z#]", ascii_part):
        return ascii_part[:8]
    latin = _latin_abbrev_from_chinese(raw)
    if len(latin) >= 2 and not re.fullmatch(r"\d+", latin):
        return latin
    m = re.match(r"^(\d+)(.*)$", re.sub(r"[线#]", "", raw))
    if m:
        num, rest = m.group(1), m.group(2)
        rest_abbr = ""
        if rest:
            rest_abbr = _latin_abbrev_from_chinese(rest) or room_abbrev(rest, registry=registry)
            if rest_abbr in ("RM", num) or re.fullmatch(r"\d+", rest_abbr or ""):
                rest_abbr = _latin_abbrev_from_chinese(rest) or "R"
        # 数字前缀 + 拉丁后缀，避免纯数字缩写与纤芯粘连
        out = f"{num}{rest_abbr}"[:10] if rest_abbr else f"{num}R"
        return out
    for k, v in sorted(ENDPOINT_ABBREVS.items(), key=lambda x: -len(x[0])):
        if k and k in raw:
            return v
    return "RM"


def assign_room_abbrevs(rooms: list[str]) -> dict[str, str]:
    """为机房列表分配唯一拉丁缩写（端口名用）。"""
    assigned: dict[str, str] = {}
    used: set[str] = set()
    for room in sorted(set(asset_room_name(r) for r in rooms if asset_room_name(r))):
        base = room_abbrev(room, registry=assigned)
        if not re.fullmatch(r"[A-Z0-9#]+", base):
            base = _latin_abbrev_from_chinese(room) or "RM"
        abbr = base
        n = 2
        while abbr in used:
            abbr = f"{base}{n}"
            n += 1
        assigned[room] = abbr
        used.add(abbr)
    return assigned


def room_route_short(name: str) -> str:
    """轧钢机房 -> 轧钢；白灰窑2机房 -> 白灰窑2"""
    room = normalize_room_name(name)
    if room.endswith("机房"):
        return room[:-2]
    if "机房" in room:
        return room.replace("机房", "").strip("- ")
    return room.split("-")[0] if "-" in room else room


def location_short_name(name: str) -> str:
    """缆段旧格式 CBL-{简称} 用位置简称（不含「机房」）。"""
    room = normalize_room_name(name)
    if not room:
        return ""
    for key in (
        "烧结主控",
        "白灰窑",
        "轧钢",
        "炼钢主控",
        "得丰",
        "新质检",
        "中唐大库",
    ):
        if key in room:
            return key
    s = re.sub(r"(机房|楼|主控室|主控|中心|三楼|二楼)$", "", room)
    return s[:8] if s else room[:8]


def infer_route_suffix(note: str) -> str:
    """从备注推断缆段/ODF 用途后缀。"""
    note = (note or "").strip()
    for kw in ROUTE_SUFFIXES:
        if kw in note:
            return kw
    return ""


def _strip_route_suffix(label: str) -> tuple[str, str]:
    body = (label or "").strip()
    for kw in ROUTE_SUFFIXES:
        if body.endswith(kw):
            return body[: -len(kw)], kw
    return body, ""


def parse_route_label(label: str) -> dict[str, str]:
    """解析 得丰机房至轧钢机房[备用] 或旧 CBL-轧钢-白灰窑。"""
    label = (label or "").strip()
    if not label:
        return {"room_a": "", "room_b": "", "suffix": "", "body": ""}
    if label.upper().startswith("CBL-"):
        parts = [p for p in label[4:].split("-") if p]
        if len(parts) >= 2:
            return {
                "room_a": parts[0],
                "room_b": parts[1],
                "suffix": parts[2] if len(parts) > 2 else "",
                "body": label,
            }
        return {"room_a": label, "room_b": "", "suffix": "", "body": label}
    body, suffix = _strip_route_suffix(label)
    if "至" in body:
        a, b = body.split("至", 1)
        return {"room_a": a.strip(), "room_b": b.strip(), "suffix": suffix, "body": body}
    return {"room_a": label, "room_b": "", "suffix": suffix, "body": body}


def is_trunk_cable_label(label: str) -> bool:
    """是否为外线缆段 rear-port 名称（新/旧格式）。"""
    label = (label or "").strip()
    if not label:
        return False
    if label.upper().startswith("CBL-"):
        return True
    p = parse_route_label(label)
    return bool(p["room_a"] and p["room_b"] and "至" in label)


def is_odf_device_name(name: str) -> bool:
    """ODF 设备名：旧 {机房}-ODF-{序号} 或新 {本端}至{对端}。"""
    name = (name or "").strip()
    if not name:
        return False
    if "ODF" in name.upper():
        return True
    p = parse_route_label(name)
    return bool(p["room_a"] and p["room_b"] and "至" in name)


def cable_belongs_on_route_odf(odf_name: str, cable_label: str) -> bool:
    """Route-style ODF (A至B) only owns rear cables on the same endpoint pair."""
    odf_name = (odf_name or "").strip()
    cable_label = (cable_label or "").strip()
    if not odf_name or not cable_label:
        return True
    if odf_name == cable_label or mirror_cable_label(cable_label) == odf_name:
        return True
    if "至" not in odf_name:
        return True
    p_dev = parse_route_label(odf_name)
    p_cab = parse_route_label(cable_label)
    if not p_dev.get("room_a") or not p_dev.get("room_b"):
        return True
    if not p_cab.get("room_a") or not p_cab.get("room_b"):
        return True
    dev_pair = {normalize_room_name(p_dev["room_a"]), normalize_room_name(p_dev["room_b"])}
    cab_pair = {normalize_room_name(p_cab["room_a"]), normalize_room_name(p_cab["room_b"])}
    return cab_pair == dev_pair


def is_bootstrap_tmp_cable(label: str) -> bool:
    """导入/bootstrap 时创建的临时 rear-port 名，非业务缆段。"""
    return (label or "").strip().upper().startswith("TMP-")


def format_route_name(room_a: str, room_b: str, *, suffix: str = "", use_full_room: bool = True) -> str:
    """{机房A}至{机房B}[后缀]。"""
    if use_full_room:
        a = asset_room_name(room_a)
        b = asset_room_name(room_b)
    else:
        a = room_route_short(room_a)
        b = room_route_short(room_b)
    suffix = (suffix or "").strip()
    return f"{a}至{b}{suffix}"


def format_cable_label(room_a: str, room_b: str, *, suffix: str = "", from_room: str = "") -> str:
    """缆段编号：按光纤走向 起点→终点，可附 备用。"""
    a = asset_room_name(room_a)
    b = asset_room_name(room_b)
    suffix = (suffix or "").strip()
    if from_room:
        local = asset_room_name(from_room)
        if local == b:
            a, b = b, a
    return format_route_name(a, b, suffix=suffix, use_full_room=True)


def format_odf_device_name(local_room: str, peer_room: str, *, suffix: str = "") -> str:
    """ODF 设备全名：本端机房至对端机房[后缀]。"""
    return format_route_name(local_room, peer_room, suffix=suffix, use_full_room=True)


def local_room_from_odf_name(odf_name: str) -> str:
    """从 ODF 设备名提取本端机房。"""
    p = parse_route_label(odf_name)
    if p["room_a"]:
        return normalize_room_name(p["room_a"])
    legacy = re.match(r"^(.+?)-([A-Za-z0-9]+)-ODF-(\d+)$", (odf_name or "").strip())
    if legacy:
        return normalize_room_name(legacy.group(1))
    return normalize_room_name((odf_name or "").split("-")[0])


def peer_room_from_odf_name(odf_name: str) -> str:
    """从 ODF 设备名提取对端机房。"""
    p = parse_route_label(odf_name)
    if p["room_b"]:
        return normalize_room_name(p["room_b"])
    return ""


def _rooms_match(a: str, b: str) -> bool:
    sa, sb = room_route_short(a), room_route_short(b)
    if not sa or not sb:
        return False
    return sa == sb or sa.startswith(sb) or sb.startswith(sa) or sa in b or sb in a


def cable_port_prefix(cable_label: str, local_room: str = "", *, abbrev_registry: dict[str, str] | None = None) -> str:
    """缆段 -> 本端端口前缀。新：DFtoXZJ；旧：ZGtoBHY。"""
    lbl = (cable_label or "").strip()
    if not lbl:
        return ""
    p = parse_route_label(lbl)
    if p["room_a"] and p["room_b"] and "至" in lbl:
        ra = asset_room_name(p["room_a"])
        rb = asset_room_name(p["room_b"])
        aa = room_abbrev(ra, registry=abbrev_registry)
        ab = room_abbrev(rb, registry=abbrev_registry)
        if local_room:
            local = asset_room_name(local_room)
            if _rooms_match(local, ra):
                return f"{aa}to{ab}"
            if _rooms_match(local, rb):
                return f"{ab}to{aa}"
        return f"{aa}to{ab}"
    if lbl.upper().startswith("CBL-"):
        inner = lbl[4:]
        parts = [x for x in inner.split("-") if x]
        if len(parts) >= 2:
            return f"{endpoint_abbrev(parts[0])}to{endpoint_abbrev(parts[1])}"
    return endpoint_abbrev(lbl.replace("-", ""))


def mirror_cable_label(cable_label: str) -> str:
    """得丰机房至轧钢机房 <-> 轧钢机房至得丰机房（保留后缀）。"""
    lbl = (cable_label or "").strip()
    p = parse_route_label(lbl)
    if p["room_a"] and p["room_b"] and "至" in lbl:
        return format_route_name(p["room_b"], p["room_a"], suffix=p["suffix"], use_full_room=True)
    if lbl.upper().startswith("CBL-"):
        inner = lbl[4:]
        parts = [x for x in inner.split("-") if x]
        if len(parts) >= 2:
            return "CBL-" + "-".join(reversed(parts[:2])) + (("-" + parts[2]) if len(parts) > 2 else "")
    return cable_label


def cable_label_variants(cable_label: str) -> list[str]:
    cable_label = (cable_label or "").strip()
    if not cable_label:
        return []
    mirror = mirror_cable_label(cable_label)
    if mirror and mirror != cable_label:
        return [cable_label, mirror]
    return [cable_label]


def endpoint_abbrev(name: str) -> str:
    """Map cable endpoint segment to English abbreviation (legacy CBL)."""
    key = (name or "").strip()
    if not key:
        return ""
    if key in ENDPOINT_ABBREVS:
        return ENDPOINT_ABBREVS[key]
    ascii_part = re.sub(r"[^a-zA-Z0-9]", "", key).upper()
    return ascii_part[:8] if ascii_part else key[:4]


def fiber_from_position(pos: int, *, cores_per_row: int = 12) -> str:
    """FMS strand position -> 排-芯 label (1-1..1-12, 2-1..2-12, …)."""
    if pos <= 0:
        return "1-1"
    row = (pos - 1) // cores_per_row + 1
    core = (pos - 1) % cores_per_row + 1
    return f"{row}-{core}"


def normalize_fiber_label(fiber: str, *, cores_per_row: int = 12) -> str:
    """Validate and normalize 排-芯 notation."""
    fiber = (fiber or "").strip()
    if not fiber:
        return "1-1"
    if fiber.isdigit():
        pos = int(fiber)
        return fiber_from_position(pos, cores_per_row=cores_per_row)
    parts = fiber.split("-", 1)
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        row, core = int(parts[0]), int(parts[1])
        if 1 <= core <= cores_per_row and 1 <= row <= 48:
            return f"{row}-{core}"
        # Recover glued prefixes like "9001-6" -> "1-6"
        m = re.search(r"(\d{1,2})-(\d{1,2})$", fiber)
        if m:
            row, core = int(m.group(1)), int(m.group(2))
            if 1 <= core <= cores_per_row and 1 <= row <= 48:
                return f"{row}-{core}"
    return fiber


def _known_port_abbrevs() -> set[str]:
    """缩写表 + 历史纯数字前缀（存量口名 900to900…）。

    勿加入 LT/RM 等可被更长已知缩写（LT1）前缀匹配的短词。
    """
    vals = {str(v).upper() for v in ENDPOINT_ABBREVS.values() if v}
    vals.update({"900", "10", "34", "2#"})
    return vals


def _split_abbrev_fiber_tail(right: str) -> list[tuple[str, str]]:
    """从 AbbrB+纤芯 右侧切分出候选 (abbr, fiber)。"""
    right = (right or "").strip()
    out: list[tuple[str, str]] = []
    m = re.match(r"^(.*?)(\d{1,2})-(\d{1,2})$", right)
    if not m:
        return out
    head, row_s, core_s = m.group(1), m.group(2), m.group(3)
    # 纤芯排号可能占 1～2 位；多出来的数字属于缩写尾部
    digit_run = row_s
    for take in range(1, len(digit_run) + 1):
        row = digit_run[-take:]
        abbr_extra = digit_run[:-take]
        abbr = f"{head}{abbr_extra}"
        if not abbr:
            continue
        fiber = f"{int(row)}-{int(core_s)}"
        row_i, core_i = int(row), int(core_s)
        if 1 <= core_i <= 12 and 1 <= row_i <= 48:
            out.append((abbr, fiber))
    # 去重保序
    seen: set[tuple[str, str]] = set()
    uniq: list[tuple[str, str]] = []
    for item in out:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


def _score_xtoy_parse(abbr_a: str, abbr_b: str, fiber: str, known: set[str]) -> int:
    aa, ab = abbr_a.upper(), abbr_b.upper()
    score = 0
    if aa in known:
        score += 80
    if ab in known:
        score += 120
    # 仅对未知缩写给「字母结尾」加分，避免 LT 压过已知 LT1
    if ab not in known and re.search(r"[A-Z#]$", ab):
        score += 15
    if aa not in known and re.search(r"[A-Z#]$", aa):
        score += 5
    if re.fullmatch(r"\d+", ab):
        score += 40
    if re.fullmatch(r"\d+", aa):
        score += 20
    # 更长的 B 缩写优先（LT1 > LT）
    score += min(len(ab), 12)
    row = int(fiber.split("-")[0])
    if 1 <= row <= 4:
        score += 3
    return score


def fiber_from_xtoy_port_name(name: str) -> str | None:
    """解析 {A}to{B}{排}-{芯}；B 以数字结尾时枚举候选并按已知缩写打分。"""
    name = (name or "").strip()
    m = re.fullmatch(r"([A-Za-z0-9#]+)to([A-Za-z0-9#]+\d*-\d+)", name, re.I)
    if not m:
        return None
    abbr_a, right = m.group(1), m.group(2)
    cands = _split_abbrev_fiber_tail(right)
    if not cands:
        return None
    known = _known_port_abbrevs()
    best: tuple[int, str] | None = None
    for abbr_b, fiber in cands:
        sc = _score_xtoy_parse(abbr_a, abbr_b, fiber, known)
        if best is None or sc > best[0]:
            best = (sc, fiber)
    return normalize_fiber_label(best[1]) if best else None


def fiber_from_port_name(name: str, cable_label: str = "", *, local_room: str = "") -> str | None:
    """Extract 排-芯 from front-port name.

    标准口名 {A}to{B}{排}-{芯} 无分隔符。当 B 以数字结尾时会粘连，
    如 PMtoLT1 + 1-1 → PMtoLT11-1、900to900 + 12-12 → 900to90012-12。
    有缆段时先剥前缀；否则按 XtoY 候选 + 已知缩写打分解析。
    """
    name = (name or "").strip()
    if not name:
        return None
    cable_label = (cable_label or "").strip()
    if cable_label:
        for variant in cable_label_variants(cable_label) or [cable_label]:
            for room in ((local_room or "").strip(), ""):
                pfx = cable_port_prefix(variant, room)
                if pfx and name.startswith(pfx):
                    tail = name[len(pfx) :]
                    if re.fullmatch(r"\d+-\d+", tail):
                        return normalize_fiber_label(tail)
    # Legacy B 端：front 口挂在对端缆段 rear 上，如 烧结白灰主控楼至解冻库:F1
    m = re.search(r":F(\d+)$", name, re.I)
    if m:
        return fiber_from_position(int(m.group(1)))
    m = re.fullmatch(r"[Ff](\d+)", name)
    if m:
        return fiber_from_position(int(m.group(1)))
    parsed = fiber_from_xtoy_port_name(name)
    if parsed:
        return parsed
    m = re.search(r"[Ff](\d+)$", name)
    if m:
        return fiber_from_position(int(m.group(1)))
    # 粘连残片或纯纤芯：9001-6 / 1-6
    m = re.search(r"(\d{1,2})-(\d{1,2})$", name)
    if not m:
        return None
    return normalize_fiber_label(f"{int(m.group(1))}-{int(m.group(2))}")


def fiber_sort_key(fiber: str) -> tuple[int, ...]:
    """Numeric sort for 排-芯 labels (1-9 before 1-10)."""
    fiber = normalize_fiber_label((fiber or "").strip())
    if not fiber:
        return (9999, 9999)
    parts = fiber.split("-")
    out: list[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            return (9999, 9999)
    return tuple(out)


def port_name_for_cable_fiber(
    cable_label: str,
    fiber: str,
    *,
    local_room: str = "",
    abbrev_registry: dict[str, str] | None = None,
) -> str:
    """得丰机房至轧钢机房 + 本端得丰 + 1-3 -> DFtoZG1-3"""
    fiber = normalize_fiber_label((fiber or "").strip())
    if not (cable_label or "").strip():
        return fiber
    return f"{cable_port_prefix(cable_label.strip(), local_room, abbrev_registry=abbrev_registry)}{fiber}"


def position_from_fiber(fiber: str, *, cores_per_row: int = 12) -> int:
    """Map 排-芯 to FMS strand position (1-based). 1-3->3, 2-1->13."""
    fiber = normalize_fiber_label(fiber, cores_per_row=cores_per_row)
    if fiber.isdigit():
        return int(fiber)
    parts = fiber.split("-", 1)
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return 1
    row, core = int(parts[0]), int(parts[1])
    if core < 1 or core > cores_per_row or row < 1:
        return 1
    return (row - 1) * cores_per_row + core


def legacy_diagonal_fiber(fiber: str, *, cores_per_row: int = 12) -> str:
    """Old wrong naming: position N was labeled N-N instead of 1-N."""
    pos = position_from_fiber(fiber, cores_per_row=cores_per_row)
    if 1 <= pos <= cores_per_row:
        return f"{pos}-{pos}"
    return normalize_fiber_label(fiber, cores_per_row=cores_per_row)


def site_slug(name: str) -> str:
    if name in SITE_SLUGS:
        return SITE_SLUGS[name]
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return ascii_part[:50] if ascii_part else "site"


def location_slug(name: str) -> str:
    """NetBox Location slug；始终带哈希后缀，避免 炼钢/炼铁110KV 等冲突。"""
    import hashlib

    name = (name or "").strip()
    if not name:
        return "location"
    if name in SITE_SLUGS:
        return SITE_SLUGS[name]
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    abbr = room_abbrev(name).lower()
    abbr = re.sub(r"[^a-z0-9]+", "", abbr)[:12]
    if abbr and abbr != "rm":
        return f"{abbr}-{digest}"[:50]
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    if len(ascii_part) >= 3:
        return f"{ascii_part}-{digest}"[:50]
    return f"loc-{digest}"[:50]


def normalize_cabinet_code(raw: str, *, default: str = "JG01") -> str:
    """机柜号规范为 {字母}{两位数字}，如 JG01、A01（登记用，不参与 ODF 设备名）。"""
    s = (raw or "").strip().upper()
    if not s:
        return default
    m = re.match(r"^([A-Z]+)(\d+)$", s)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}"
    return s


def default_cabinet_for_room(room: str) -> str:
    """按机房类型给出默认机柜号。"""
    room = normalize_room_name(room)
    if "白灰窑" in room or "烧结主控" in room:
        return "A01"
    return "JG01"
