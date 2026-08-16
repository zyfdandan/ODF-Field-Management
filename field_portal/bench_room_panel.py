"""Quick benchmark for get_room_port_panel."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from field_service import client_from_config, load_config
from field_browser import get_room_port_panel, invalidate_room_panel_cache

SITE = "示例站点"
ROOM = "示例机房"
WEB = ""  # 留空则从 netbox_config.json 的 web_base_url 读取


def main() -> None:
    client = client_from_config(load_config())
    cfg = load_config()
    web = WEB or str(cfg.get("web_base_url") or "").rstrip("/")
    if not web:
        raise SystemExit("请在脚本中设置 WEB，或在 netbox_config.json 填写 web_base_url")
    invalidate_room_panel_cache()
    t0 = time.time()
    r = get_room_port_panel(client, SITE, ROOM, web, force_refresh=True)
    cold = time.time() - t0
    t0 = time.time()
    get_room_port_panel(client, SITE, ROOM, web)
    warm = time.time() - t0
    print(f"routes={r['route_count']} cold={cold:.2f}s warm={warm:.2f}s")


if __name__ == "__main__":
    main()
