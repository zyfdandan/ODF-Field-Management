#!/usr/bin/env python3
"""NetBox device ImageAttachment helpers for Field Portal."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

OBJECT_TYPE = "dcim.device"


def parse_device_id(raw: str | int | None) -> int:
    try:
        did = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("device_id 无效") from None
    if did <= 0:
        raise ValueError("device_id 无效")
    return did


def parse_attachment_id(raw: str | int | None) -> int:
    try:
        aid = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("attachment_id 无效") from None
    if aid <= 0:
        raise ValueError("attachment_id 无效")
    return aid


def _web_root(client) -> str:
    """NetBox web origin from API base_url (…/api → …)."""
    base = getattr(client, "base_url", "") or ""
    if base.rstrip("/").endswith("/api"):
        return base.rstrip("/")[: -len("/api")]
    return base.rstrip("/")


def media_url_from_attachment(att: dict[str, Any], web_root: str) -> str:
    """Resolve a fetchable absolute media URL from a NetBox attachment dict."""
    raw = (
        att.get("image_url")
        or att.get("image")
        or (att.get("image_path") if isinstance(att.get("image_path"), str) else None)
        or ""
    )
    if isinstance(raw, dict):
        raw = raw.get("url") or raw.get("path") or ""
    raw = str(raw or "").strip()
    if not raw:
        raise ValueError("附件无图片地址")
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    root = web_root.rstrip("/") + "/"
    if raw.startswith("/"):
        # origin only
        p = urlparse(web_root)
        origin = f"{p.scheme}://{p.netloc}"
        return origin + raw
    return urljoin(root, raw)


def normalize_attachment(att: dict[str, Any]) -> dict[str, Any]:
    aid = int(att["id"])
    return {
        "id": aid,
        "name": (att.get("name") or "").strip(),
        "file_url": f"/api/device-images/{aid}/file",
    }


def list_device_images(client, device_id: int) -> dict[str, Any]:
    did = parse_device_id(device_id)
    data = client.request(
        "GET",
        "/extras/image-attachments/",
        params={"object_type": OBJECT_TYPE, "object_id": did, "limit": 100},
    )
    results = [normalize_attachment(r) for r in (data.get("results") or [])]
    return {"device_id": did, "count": len(results), "results": results}


def get_attachment(client, attachment_id: int) -> dict[str, Any]:
    aid = parse_attachment_id(attachment_id)
    return client.request("GET", f"/extras/image-attachments/{aid}/")


def fetch_attachment_bytes(client, attachment_id: int) -> tuple[bytes, str]:
    """Return (body, content_type). Uses client.session with auth; clears JSON Content-Type."""
    att = get_attachment(client, attachment_id)
    url = media_url_from_attachment(att, _web_root(client))
    headers = {"Accept": "*/*"}
    # Avoid forcing application/json on binary GET
    resp = client.session.get(url, headers=headers, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"拉取图片失败 HTTP {resp.status_code}")
    ctype = resp.headers.get("Content-Type") or "application/octet-stream"
    return resp.content, ctype.split(";")[0].strip()


def upload_device_image(
    client,
    device_id: int,
    *,
    filename: str,
    content: bytes,
    name: str = "",
    content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    did = parse_device_id(device_id)
    if not content:
        raise ValueError("空文件")
    fname = (filename or "upload.jpg").strip() or "upload.jpg"
    data = {
        "object_type": OBJECT_TYPE,
        "object_id": str(did),
    }
    if name.strip():
        data["name"] = name.strip()
    files = {"image": (fname, content, content_type or "application/octet-stream")}
    # NetBoxClient session defaults to Content-Type: application/json. That header
    # survives a filtered headers= dict and makes NetBox parse the multipart JPEG
    # as JSON (utf-8 0xff errors). Setting None removes it so requests can set boundary.
    headers = {k: v for k, v in client.session.headers.items() if k.lower() != "content-type"}
    headers["Content-Type"] = None
    url = f"{client.base_url.rstrip('/')}/extras/image-attachments/"
    resp = client.session.post(url, data=data, files=files, headers=headers, timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"上传失败 HTTP {resp.status_code}: {resp.text[:500]}")
    att = resp.json()
    return normalize_attachment(att)


def delete_device_image(client, attachment_id: int) -> dict[str, Any]:
    """Delete a NetBox ImageAttachment by id."""
    aid = parse_attachment_id(attachment_id)
    url = f"{client.base_url.rstrip('/')}/extras/image-attachments/{aid}/"
    resp = client.session.delete(url, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"删除失败 HTTP {resp.status_code}: {resp.text[:500]}")
    return {"ok": True, "id": aid}
