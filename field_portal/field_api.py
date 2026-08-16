#!/usr/bin/env python3
"""HTTP API for ODF field registration (Node-RED proxy target)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from field_audit import client_ip, device_from_headers, log_path, read_audit, write_audit
from field_browser import get_location_tree, get_odf_modal_panel, get_odf_port_panel, get_room_port_panel
from field_locks import acquire_lock, assert_editable, lock_status_public, release_lock, renew_lock
from field_service import (
    client_from_config,
    finalize_circuit,
    get_odf_context,
    get_port_business_detail,
    get_port_occupancy,
    invalidate_caches_for_webhook,
    list_device_interfaces,
    load_config,
    rename_circuit_business_name,
    submit_loss,
    submit_segment,
    release_port_registration,
    swap_segment_fiber,
)

FORM_PATH = Path(__file__).with_name("form.html")
TRUNK_FORM_PATH = Path(__file__).with_name("trunk_form.html")
INDEX_PATH = Path(__file__).with_name("index.html")
AUDIT_PATH = Path(__file__).with_name("audit.html")


def portal_config() -> dict:
    from portal_settings import portal_config as _cfg

    return _cfg()


class Handler(BaseHTTPRequestHandler):
    config: dict = {}

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, payload: dict, *, no_store: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        if no_store:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.exists():
            self._json(404, {"error": "not found"})
            return
        self._html(200, path.read_text(encoding="utf-8"))

    def _audit(self, action: str, *, path: str = "", detail: dict | None = None, ok: bool = True, error: str = "", device_extra: dict | None = None) -> None:
        try:
            write_audit(
                action=action,
                ip=client_ip(self),
                path=path or (urlparse(self.path).path or ""),
                method=self.command,
                device=device_from_headers(self, device_extra),
                detail=detail,
                ok=ok,
                error=error,
            )
        except Exception as exc:
            sys.stderr.write(f"WARN: audit write failed: {exc}\n")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        from field_warmup import request_gate

        with request_gate(urlparse(self.path).path):
            self._do_GET()

    def _do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        cfg = {**load_config(), **self.config}
        web_base = cfg.get("web_base_url", cfg["base_url"].replace("/api", ""))

        if path in ("/", "/portal", "/index.html"):
            self._audit("page_view", path=path, detail={"page": "portal"})
            self._file(INDEX_PATH)
            return

        if path in ("/form", "/odf"):
            self._audit("page_view", path=path, detail={"page": "form", "odf": (qs.get("odf") or [""])[0]})
            self._file(FORM_PATH)
            return

        if path in ("/trunk", "/trunk-form"):
            self._audit("page_view", path=path, detail={"page": "trunk"})
            self._file(TRUNK_FORM_PATH)
            return

        if path in ("/audit", "/audit.html", "/api/audit-page", "/api/audit/page"):
            self._audit("audit_view", path=path, detail={"page": "audit"})
            self._file(AUDIT_PATH)
            return

        if path in ("/api/audit", "/audit/list"):
            try:
                limit = int((qs.get("limit") or ["200"])[0] or 200)
            except ValueError:
                limit = 200
            action = (qs.get("action") or [""])[0].strip()
            ip = (qs.get("ip") or [""])[0].strip()
            rows = read_audit(limit=limit, action=action, ip=ip)
            self._json(
                200,
                {"rows": rows, "log_file": str(log_path()), "count": len(rows)},
                no_store=True,
            )
            return

        if path == "/health":
            self._json(200, {"status": "ok"})
            return

        if path in ("/cache/status", "/api/cache/status"):
            self._json(200, self._cache_status_payload(), no_store=True)
            return

        if path in ("/cache/refresh", "/api/cache/refresh"):
            mode = (qs.get("mode") or [""])[0].strip()
            start = (qs.get("start") or [""])[0].lower() in ("1", "true", "yes")
            if not mode and not start:
                self._json(400, {"error": "缺少 mode（occupancy 或 full）"}, no_store=True)
                return
            self._handle_cache_refresh(mode=mode or "occupancy")
            return

        if path in ("/api/edit-lock", "/edit-lock"):
            odf = (qs.get("odf") or [""])[0].strip()
            cable = (qs.get("cable") or [""])[0].strip()
            fiber = (qs.get("fiber") or [""])[0].strip()
            if not odf or not fiber:
                self._json(400, {"error": "缺少 odf 或 fiber"})
                return
            self._json(200, lock_status_public(odf, cable, fiber, ip=client_ip(self)), no_store=True)
            return

        try:
            client = client_from_config(cfg)
        except Exception as e:
            self._json(500, {"error": str(e)})
            return

        if path in ("/api/tree", "/tree") or path.endswith("/tree"):
            try:
                refresh = (qs.get("refresh") or [""])[0].lower() in ("1", "true", "yes")
                self._json(200, get_location_tree(client, force_refresh=refresh), no_store=True)
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/api/odf-panel", "/odf-panel") or path.endswith("/odf-panel"):
            odf = (qs.get("odf") or [""])[0]
            cable = (qs.get("cable") or [""])[0]
            modal = (qs.get("modal") or [""])[0].lower() in ("1", "true", "yes")
            if not odf:
                self._json(400, {"error": "缺少 odf 参数"})
                return
            try:
                if modal and cable:
                    self._json(200, get_odf_modal_panel(client, odf, web_base, cable))
                else:
                    self._json(200, get_odf_port_panel(client, odf, web_base, cable_filter=cable))
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/api/room-panel", "/room-panel") or path.endswith("/room-panel"):
            site = (qs.get("site") or [""])[0]
            room = (qs.get("room") or [""])[0]
            if not site or not room:
                self._json(400, {"error": "缺少 site 或 room 参数"})
                return
            try:
                refresh = (qs.get("refresh") or [""])[0].lower() in ("1", "true", "yes")
                fast = (qs.get("fast") or [""])[0].lower() in ("1", "true", "yes")
                occupancy = (qs.get("occupancy") or [""])[0].lower() in ("1", "true", "yes")
                if refresh:
                    from field_browser import invalidate_room_panel_cache

                    invalidate_room_panel_cache(site=site, room=room)
                    if occupancy or not fast:
                        from field_service import invalidate_fp_circuit_index_cache

                        invalidate_fp_circuit_index_cache()
                if refresh and not fast and not occupancy:
                    occupancy = True
                self._json(
                    200,
                    get_room_port_panel(
                        client,
                        site,
                        room,
                        web_base,
                        force_refresh=refresh and occupancy,
                        fast=fast,
                        occupancy_only=occupancy,
                    ),
                )
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/api/port-status", "/port-status") or path.endswith("/port-status"):
            odf = (qs.get("odf") or [""])[0]
            cable = (qs.get("cable") or [""])[0]
            fiber = (qs.get("fiber") or [""])[0]
            fp_raw = (qs.get("fp_id") or [""])[0]
            fp_id = int(fp_raw) if fp_raw.isdigit() else None
            refresh = (qs.get("refresh") or [""])[0].lower() in ("1", "true", "yes")
            if not odf or not fiber:
                self._json(400, {"error": "缺少 odf 或 fiber 参数"})
                return
            try:
                self._json(
                    200,
                    get_port_occupancy(client, odf, fp_id, cable, fiber, web_base, force_refresh=refresh),
                )
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/api/room-order", "/room-order") or path.endswith("/room-order"):
            site = (qs.get("site") or [""])[0]
            room = (qs.get("room") or [""])[0]
            if not site or not room:
                self._json(400, {"error": "缺少 site 或 room 参数"})
                return
            try:
                from room_layout import get_room_order

                self._json(200, {"site": site, "room": room, "order": get_room_order(site, room)})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/api/device-interfaces", "/device-interfaces") or path.endswith("/device-interfaces"):
            device = (qs.get("device") or [""])[0]
            if not device:
                self._json(400, {"error": "缺少 device 参数"})
                return
            try:
                self._json(200, {"device": device, "interfaces": list_device_interfaces(client, device)})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/api/port-detail", "/port-detail") or path.endswith("/port-detail"):
            odf = (qs.get("odf") or [""])[0]
            cable = (qs.get("cable") or [""])[0]
            fiber = (qs.get("fiber") or [""])[0]
            fp_raw = (qs.get("fp_id") or [""])[0]
            fp_id = int(fp_raw) if fp_raw.isdigit() else None
            if not odf or not fiber:
                self._json(400, {"error": "缺少 odf 或 fiber 参数"})
                return
            try:
                self._json(200, get_port_business_detail(client, odf, fp_id, cable, fiber, web_base))
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/api/trunk-context", "/trunk-context") or path.endswith("/trunk-context"):
            try:
                from trunk_infra import list_trunk_context

                self._json(200, list_trunk_context(client))
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        if path in ("/api/trunk-preview", "/trunk-preview") or path.endswith("/trunk-preview"):
            try:
                from trunk_infra import build_trunk_plan

                plan = build_trunk_plan(
                    client,
                    site=(qs.get("site") or [""])[0],
                    room_a=(qs.get("room_a") or [""])[0],
                    room_b=(qs.get("room_b") or [""])[0],
                    room_a_new=(qs.get("room_a_new") or ["0"])[0].lower() in ("1", "true", "yes"),
                    room_b_new=(qs.get("room_b_new") or ["0"])[0].lower() in ("1", "true", "yes"),
                    room_a_text=(qs.get("room_a_text") or [""])[0],
                    room_b_text=(qs.get("room_b_text") or [""])[0],
                    cores=(qs.get("cores") or ["12"])[0],
                    length_m=(qs.get("length_m") or qs.get("length") or [""])[0],
                    connector_a=(qs.get("connector_a") or qs.get("conn_a") or [""])[0],
                    connector_b=(qs.get("connector_b") or qs.get("conn_b") or [""])[0],
                )
                self._json(200, {"plan": plan})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return

        if path.endswith("/context") or path == "/api/context":
            odf = (qs.get("odf") or [""])[0]
            cable = (qs.get("cable") or [""])[0]
            if not odf:
                self._json(400, {"error": "缺少 odf 参数"})
                return
            try:
                from field_service import get_odf_context

                self._json(200, get_odf_context(client, odf, web_base, cable_filter=cable))
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        from field_warmup import request_gate

        with request_gate(urlparse(self.path).path):
            self._do_POST()

    def _do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path in ("/cache/invalidate", "/api/cache/invalidate"):
            self._handle_cache_invalidate()
            return

        if path in ("/cache/refresh", "/api/cache/refresh"):
            length = int(self.headers.get("Content-Length", 0))
            payload: dict = {}
            if length:
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._json(400, {"error": "invalid json"})
                    return
                if not isinstance(payload, dict):
                    payload = {}
            self._handle_cache_refresh(mode=(payload.get("mode") or "occupancy"))
            return

        # Optional richer device info from browser (IP still from server)
        if path in ("/api/audit/beacon", "/audit/beacon"):
            length = int(self.headers.get("Content-Length", 0))
            extra = {}
            try:
                if length:
                    extra = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return
            if not isinstance(extra, dict):
                extra = {}
            self._audit(
                "beacon",
                path=path,
                detail={"page": (extra.get("page") or "")[:80]},
                device_extra=extra,
            )
            self._json(200, {"ok": True})
            return

        if not (
            path.endswith("/submit")
            or path == "/api/submit"
            or path.endswith("/room-order")
            or path == "/api/room-order"
            or path.endswith("/edit-lock")
            or path == "/api/edit-lock"
        ):
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self._audit("submit", path=path, ok=False, error="invalid json")
            self._json(400, {"error": "invalid json"})
            return

        cfg = {**load_config(), **self.config}
        action = (payload.get("action") or "segment").lower()
        device_extra = payload.get("device_info") if isinstance(payload.get("device_info"), dict) else None

        # Plan B: soft edit locks (also via /api/submit action=edit_*)
        if path.endswith("/edit-lock") or path == "/api/edit-lock" or action in (
            "edit_lock",
            "edit_renew",
            "edit_unlock",
            "lock",
            "unlock",
            "lock_renew",
        ):
            lock_action = action
            if path.endswith("/edit-lock") or path == "/api/edit-lock":
                lock_action = (payload.get("op") or payload.get("action") or "acquire").lower()
            ip = client_ip(self)
            ua = (self.headers.get("User-Agent") or "")
            odf = (payload.get("odf") or "").strip()
            cable = (payload.get("cable") or "").strip()
            fiber = (payload.get("fiber") or payload.get("port_local") or payload.get("port") or "").strip()
            token = (payload.get("lock_token") or payload.get("token") or "").strip()
            force = bool(payload.get("force"))
            try:
                if lock_action in ("edit_unlock", "unlock", "release"):
                    result = release_lock(odf=odf, cable=cable, fiber=fiber, ip=ip, token=token, force=force)
                elif lock_action in ("edit_renew", "lock_renew", "renew"):
                    result = renew_lock(
                        odf=odf, cable=cable, fiber=fiber, ip=ip, token=token,
                        device=device_from_headers(self, device_extra), user_agent=ua,
                    )
                else:
                    result = acquire_lock(
                        odf=odf, cable=cable, fiber=fiber, ip=ip,
                        device=device_from_headers(self, device_extra), user_agent=ua,
                        token=token, force=force,
                    )
                code = 200 if result.get("ok", True) or result.get("mine") else 409
                if result.get("conflict") and not result.get("mine"):
                    code = 409
                self._audit(
                    "edit_lock",
                    path=path,
                    detail={"op": lock_action, "odf": odf[:120], "fiber": fiber[:40], "force": force},
                    ok=(code == 200),
                    error="" if code == 200 else (result.get("message") or "conflict"),
                    device_extra=device_extra,
                )
                self._json(code, result)
            except Exception as e:
                self._audit("edit_lock", path=path, ok=False, error=str(e), device_extra=device_extra)
                self._json(400, {"error": str(e)})
            return

        audit_action = "room_order" if (
            path.endswith("/room-order") or path == "/api/room-order" or action == "room_order"
        ) else "submit"
        detail = {
            "action": action,
            "odf": (payload.get("odf") or payload.get("device_a") or "")[:120],
            "cid": (payload.get("cid") or "")[:80],
            "site": (payload.get("site") or "")[:80],
            "room": (payload.get("room") or "")[:80],
        }
        try:
            if path.endswith("/room-order") or path == "/api/room-order" or action == "room_order":
                from room_layout import save_room_order

                site = (payload.get("site") or "").strip()
                room = (payload.get("room") or "").strip()
                order = payload.get("order") or []
                if not site or not room:
                    self._audit(audit_action, path=path, detail=detail, ok=False, error="缺少 site 或 room", device_extra=device_extra)
                    self._json(400, {"error": "缺少 site 或 room"})
                    return
                if not isinstance(order, list):
                    self._audit(audit_action, path=path, detail=detail, ok=False, error="order 必须为数组", device_extra=device_extra)
                    self._json(400, {"error": "order 必须为数组"})
                    return
                saved = save_room_order(site, room, order)
                result = {"message": "ODF 排序已保存", "site": site, "room": room, "order": saved}
                self._audit(audit_action, path=path, detail=detail, device_extra=device_extra)
                self._json(200, result)
                return

            # Enforce edit lock on mutating port actions
            if action in ("segment", "loss", "release", "swap_segment", "rename_circuit", ""):
                lock_odf = (payload.get("odf") or "").strip()
                lock_cable = (payload.get("cable") or "").strip()
                lock_fiber = (
                    payload.get("fiber")
                    or payload.get("port_local")
                    or payload.get("port")
                    or ""
                ).strip()
                if lock_odf and lock_fiber:
                    assert_editable(
                        odf=lock_odf,
                        cable=lock_cable,
                        fiber=lock_fiber,
                        ip=client_ip(self),
                        token=(payload.get("lock_token") or "").strip(),
                    )

            if action == "loss":
                result = submit_loss(cfg, payload)
            elif action == "release":
                result = release_port_registration(cfg, payload)
            elif action == "swap_segment":
                result = swap_segment_fiber(cfg, payload)
            elif action == "rename_circuit":
                result = rename_circuit_business_name(cfg, payload)
            elif action == "finalize":
                result = finalize_circuit(cfg, (payload.get("cid") or "").strip())
            elif action == "trunk":
                from trunk_infra import submit_trunk_infra

                client = client_from_config(cfg)
                result = submit_trunk_infra(client, payload)
            elif action in ("set_connector", "connector"):
                from trunk_infra import set_odf_connector

                client = client_from_config(cfg)
                result = set_odf_connector(client, payload)
            else:
                result = submit_segment(cfg, payload)
            try:
                from field_browser import invalidate_room_panel_cache

                site = (payload.get("site") or cfg.get("default_site") or "").strip()
                room = (payload.get("room") or "").strip()
                plan = (result or {}).get("plan") or {}
                if action == "trunk":
                    for rm in (plan.get("room_a"), plan.get("room_b")):
                        if site and rm:
                            invalidate_room_panel_cache(site=site, room=str(rm).strip())
                elif site or room:
                    invalidate_room_panel_cache(site=site, room=room)
                else:
                    invalidate_room_panel_cache()
            except Exception:
                pass
            # Auto-release lock after successful mutate
            try:
                lock_odf = (payload.get("odf") or "").strip()
                lock_cable = (payload.get("cable") or "").strip()
                lock_fiber = (
                    payload.get("fiber")
                    or payload.get("port_local")
                    or payload.get("port")
                    or ""
                ).strip()
                if action in ("segment", "loss", "release", "swap_segment") and lock_odf and lock_fiber:
                    release_lock(
                        odf=lock_odf,
                        cable=lock_cable,
                        fiber=lock_fiber,
                        ip=client_ip(self),
                        token=(payload.get("lock_token") or "").strip(),
                        force=False,
                    )
            except Exception:
                pass
            self._audit(audit_action, path=path, detail=detail, device_extra=device_extra)
            self._json(200, result)
        except Exception as e:
            self._audit(audit_action, path=path, detail=detail, ok=False, error=str(e), device_extra=device_extra)
            self._json(400, {"error": str(e)})

    def _cache_status_payload(self) -> dict:
        from field_cache import cache_status
        from field_warmup import warmup_progress

        try:
            status = cache_status()
        except Exception as exc:
            status = {"error": str(exc)}
        status["refresh"] = warmup_progress()
        return status

    def _handle_cache_refresh(self, *, mode: str = "occupancy") -> None:
        from field_warmup import request_cache_refresh

        try:
            result = request_cache_refresh(self.config or None, mode=mode)
        except ValueError as exc:
            self._json(400, {"error": str(exc)}, no_store=True)
            return
        except Exception as exc:
            self._audit("cache_refresh", path="/cache/refresh", ok=False, error=str(exc), detail={"mode": mode})
            self._json(500, {"error": str(exc)}, no_store=True)
            return
        accepted = bool(result.get("accepted"))
        self._audit(
            "cache_refresh",
            path="/cache/refresh",
            ok=accepted,
            detail={"mode": result.get("mode") or mode, "accepted": accepted},
        )
        self._json(202 if accepted else 409, result, no_store=True)

    def _cache_webhook_secret(self) -> str:
        secret = os.environ.get("CACHE_WEBHOOK_SECRET", "").strip()
        if secret:
            return secret
        return str(portal_config().get("cache_webhook_secret") or "").strip()

    def _handle_cache_invalidate(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        expected = self._cache_webhook_secret()
        if expected:
            got = (payload.get("secret") or self.headers.get("X-Cache-Secret") or "").strip()
            if got != expected:
                self._json(403, {"error": "invalid secret"})
                return
        target = (payload.get("target") or payload.get("scope") or "all").strip().lower()
        path_id_raw = payload.get("path_id")
        path_id = int(path_id_raw) if path_id_raw not in (None, "") else None
        site = (payload.get("site") or "").strip()
        room = (payload.get("room") or "").strip()
        try:
            invalidate_caches_for_webhook(target, path_id=path_id, site=site, room=room)
            self._json(200, {"message": "cache invalidated", "target": target})
        except Exception as e:
            self._json(500, {"error": str(e)})


def main() -> None:
    parser = argparse.ArgumentParser(description="ODF field registration API")
    parser.add_argument("--host", default=os.environ.get("FIELD_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FIELD_API_PORT", "8765")))
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    Handler.config = cfg

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        from field_warmup import start_background_warmup

        start_background_warmup(cfg)
    except Exception as exc:
        sys.stderr.write(f"WARN: cache warmup not started: {exc}\n")

    print(f"Field API http://{args.host}:{args.port}")
    print("  GET  /portal          ODF 浏览管理页")
    print("  GET  /audit           审计日志查看")
    print("  GET  /api/audit       审计日志 JSON")
    print("  POST /api/audit/beacon 设备信息上报")
    print("  GET  /api/tree        厂区-机房-ODF路由")
    print("  GET  /api/odf-panel   端口面板+光损")
    print("  GET  /cache/status    缓存状态")
    print("  POST /cache/refresh   更新占用/全量重建")
    print("  POST /cache/invalidate  缓存失效(Webhook)")
    print("  POST /api/submit      光损/业务")
    server.serve_forever()


if __name__ == "__main__":
    main()
