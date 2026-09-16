# Device Image Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a **图片** button next to each room-panel route name so field users can view and upload NetBox device image attachments via Field API (no NetBox browser login).

**Architecture:** Thin `field_images.py` helpers call NetBox `/api/extras/image-attachments/` with the existing token. Field API exposes list / file-byte proxy / multipart upload. `index.html` opens a styled modal. Node-RED must forward the new POST and binary GET (current flow only proxies JSON submit/room-order and parses GET as JSON objects).

**Tech Stack:** Python 3.10+ `ThreadingHTTPServer`, `requests`, static HTML/CSS/JS, Node-RED 3.x reverse proxy, NetBox ImageAttachment API.

**Spec:** `docs/superpowers/specs/2026-09-16-device-image-attachments-design.md`

---

## File map

| File | Responsibility |
|------|----------------|
| `field_portal/field_images.py` | **Create.** List / upload / resolve media URL / fetch bytes against NetBox |
| `field_portal/field_browser.py` | Add `device_id` on room/route panel payloads that already expose `device_url` |
| `field_portal/field_api.py` | Wire `GET/POST /api/device-images` and `GET /api/device-images/{id}/file` |
| `field_portal/index.html` | Button, modal UI, fetch/upload JS; skip drag on button |
| `field_portal/nodered_flow.json` | Proxy multipart POST + binary file GET |
| `field_portal/test_field_images.py` | **Create.** Small unit tests for URL resolution / list normalization (mocked) |

---

### Task 1: Image helper module + unit tests

**Files:**
- Create: `field_portal/field_images.py`
- Create: `field_portal/test_field_images.py`

- [ ] **Step 1: Write failing unit tests**

Create `field_portal/test_field_images.py`:

```python
#!/usr/bin/env python3
"""Unit tests for field_images helpers (no live NetBox)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from field_images import media_url_from_attachment, normalize_attachment, parse_device_id


class ParseDeviceIdTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(parse_device_id("42"), 42)

    def test_bad(self):
        with self.assertRaises(ValueError):
            parse_device_id("x")
        with self.assertRaises(ValueError):
            parse_device_id("0")


class MediaUrlTests(unittest.TestCase):
    def test_absolute_image_url(self):
        att = {"image": "http://nb/media/image-attachments/a.jpg"}
        self.assertEqual(
            media_url_from_attachment(att, "http://nb"),
            "http://nb/media/image-attachments/a.jpg",
        )

    def test_relative_image_path(self):
        att = {"image": "/media/image-attachments/a.jpg"}
        self.assertEqual(
            media_url_from_attachment(att, "http://nb"),
            "http://nb/media/image-attachments/a.jpg",
        )


class NormalizeTests(unittest.TestCase):
    def test_file_url_is_field_api_path(self):
        att = {"id": 7, "name": "front", "image": "/media/x.jpg"}
        out = normalize_attachment(att)
        self.assertEqual(out["id"], 7)
        self.assertEqual(out["name"], "front")
        self.assertEqual(out["file_url"], "/api/device-images/7/file")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — expect fail (module missing)**

Run (from `field_portal/`):

```powershell
cd C:\Users\Administrator\netbox-fiber-import\field_portal
python test_field_images.py -v
```

Expected: `ModuleNotFoundError: No module named 'field_images'`

- [ ] **Step 3: Implement `field_images.py`**

Create `field_portal/field_images.py`:

```python
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
    # NetBoxClient sets Content-Type: application/json on the session — drop it for multipart
    headers = {k: v for k, v in client.session.headers.items() if k.lower() != "content-type"}
    url = f"{client.base_url.rstrip('/')}/extras/image-attachments/"
    resp = client.session.post(url, data=data, files=files, headers=headers, timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"上传失败 HTTP {resp.status_code}: {resp.text[:500]}")
    att = resp.json()
    return normalize_attachment(att)
```

- [ ] **Step 4: Run unit tests — expect pass**

```powershell
cd C:\Users\Administrator\netbox-fiber-import\field_portal
python test_field_images.py -v
```

Expected: all OK.

- [ ] **Step 5: Commit**

```powershell
cd C:\Users\Administrator\netbox-fiber-import
git add field_portal/field_images.py field_portal/test_field_images.py
git commit -m "feat: add NetBox image attachment helpers for field portal"
```

---

### Task 2: Expose `device_id` on panel payloads

**Files:**
- Modify: `field_portal/field_browser.py` (return dicts that already include `device_url`)

- [ ] **Step 1: Add `device_id` to `_build_route_port_grid` return**

In `field_portal/field_browser.py`, in the `return { ... }` of `_build_route_port_grid` (around the dict that has `"device_url": device_url`), add:

```python
"device_id": did,
```

- [ ] **Step 2: Add `device_id` to `get_odf_modal_panel` and `get_odf_port_panel` returns**

Where those functions return `"device_url": ...`, also set:

```python
"device_id": did,
```

(Use the local `did` already resolved in each function; for modal panel prefer `int(grid.get("device_id") or did)`.)

- [ ] **Step 3: Invalidate room-panel cache so `device_id` is not stuck behind TTL**

Room panels are served from memory/disk cache (`get_room_port_panel` / `load_room_panel_from_disk`). After adding `device_id`, old cached JSON will omit the field and the UI button will stay hidden.

Do one of:

```powershell
# Preferred during dev: force refresh query
curl -s "http://127.0.0.1:8765/api/room-panel?site=SITE&room=ROOM&refresh=1" | python -c "import sys,json; d=json.load(sys.stdin); print('keys', list(d)[:8])"

# Or call invalidate helper once
cd C:\Users\Administrator\netbox-fiber-import\field_portal
python -c "from field_browser import invalidate_room_panel_cache; invalidate_room_panel_cache(); print('ok')"
```

Confirm a route object includes integer `device_id`. Room-panel payload uses `data.routes` (also exposed/aliased as `odfs` in some callers) — check e.g. `(d.get('routes') or d.get('odfs') or [])[0].get('device_id')`.

- [ ] **Step 4: Commit**

```powershell
git add field_portal/field_browser.py
git commit -m "feat: include device_id on ODF panel payloads"
```

---

### Task 3: Field API list / file / upload routes

**Files:**
- Modify: `field_portal/field_api.py`

- [ ] **Step 1: Add binary response helper and imports**

At top of `field_api.py`, add:

```python
import cgi
import re
from field_images import (
    fetch_attachment_bytes,
    list_device_images,
    parse_attachment_id,
    parse_device_id,
    upload_device_image,
)
```

(`cgi` is fine on 3.10/3.11; if unavailable, parse multipart with `email.message` — prefer `cgi.FieldStorage` for this stdlib server.)

Add method on `Handler`:

```python
def _bytes(self, code: int, body: bytes, content_type: str) -> None:
    self.send_response(code)
    self.send_header("Content-Type", content_type)
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Cache-Control", "private, max-age=60")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)
```

- [ ] **Step 2: Handle GET list and GET file in `do_GET`**

Before the final `self._json(404, ...)`, add:

```python
# GET /api/device-images?device_id=
if path in ("/api/device-images", "/device-images") or path.endswith("/device-images"):
    try:
        did = parse_device_id((qs.get("device_id") or [""])[0])
        self._json(200, list_device_images(client, did))
    except ValueError as e:
        self._json(400, {"error": str(e)})
    except Exception as e:
        self._json(502, {"error": str(e)})
    return

# GET /api/device-images/{id}/file
m = re.search(r"/device-images/(\d+)/file/?$", path)
if m:
    try:
        aid = parse_attachment_id(m.group(1))
        body, ctype = fetch_attachment_bytes(client, aid)
        self._bytes(200, body, ctype)
    except ValueError as e:
        self._json(400, {"error": str(e)})
    except Exception as e:
        self._json(502, {"error": str(e)})
    return
```

Place these **after** `client = client_from_config(cfg)` succeeds (same try block pattern as other routes).

- [ ] **Step 3: Handle POST upload in `do_POST` BEFORE the existing JSON allow-list / `json.loads` gate**

Current `do_POST` allow-lists only `/submit` and `/room-order`, then always `json.loads`. Adding `/device-images` to that list without an early branch will break multipart bodies.

At the **start** of `do_POST` (after path parse; before the submit/room-order allow-list), handle device images:

```python
import base64  # at module top with other imports

# ... inside do_POST, early:
if path.endswith("/device-images") or path == "/api/device-images":
    try:
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        cfg = {**load_config(), **self.config}
        client = client_from_config(cfg)
        if "multipart/form-data" in ctype:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": ctype,
                    "CONTENT_LENGTH": str(length),
                },
            )
            did_raw = form.getvalue("device_id")
            name = form.getvalue("name") or ""
            file_item = form["image"] if "image" in form else None
            if file_item is None or not getattr(file_item, "file", None):
                self._json(400, {"error": "缺少 image 文件"})
                return
            content = file_item.file.read()
            filename = getattr(file_item, "filename", None) or "upload.jpg"
            fctype = getattr(file_item, "type", None) or "application/octet-stream"
            result = upload_device_image(
                client,
                parse_device_id(did_raw),
                filename=filename,
                content=content,
                name=str(name or ""),
                content_type=str(fctype),
            )
        elif "application/json" in ctype:
            # Node-RED JSON proxy fallback (see Task 5)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            raw = base64.b64decode(payload.get("content_base64") or "")
            result = upload_device_image(
                client,
                parse_device_id(payload.get("device_id")),
                filename=(payload.get("filename") or "upload.jpg"),
                content=raw,
                name=str(payload.get("name") or ""),
            )
        else:
            self._json(400, {"error": "需要 multipart/form-data 或 JSON content_base64"})
            return
        self._json(200, result)
    except ValueError as e:
        self._json(400, {"error": str(e)})
    except Exception as e:
        self._json(400, {"error": str(e)})
    return

# existing submit / room-order allow-list + json.loads continues below unchanged
```

Do **not** add `/device-images` into the later allow-list that always `json.loads`.

Update `do_OPTIONS` Allow-Headers to include what browsers send for multipart if needed (usually fine with `*`-like CORS already used).

- [ ] **Step 4: Print new routes in `main()` help text**

Add lines:

```text
GET  /api/device-images?device_id=
GET  /api/device-images/{id}/file
POST /api/device-images   multipart device_id+image
```

- [ ] **Step 5: Manual API smoke (against live NetBox if available)**

```powershell
# List (replace DEVICE_ID)
curl -s "http://127.0.0.1:8765/api/device-images?device_id=DEVICE_ID"

# Upload
curl -s -F "device_id=DEVICE_ID" -F "image=@C:\path\to\test.jpg" -F "name=test" "http://127.0.0.1:8765/api/device-images"

# File bytes — confirm Content-Type is image/*
curl -sI "http://127.0.0.1:8765/api/device-images/ATTACHMENT_ID/file"
```

Expected: JSON list with `file_url` paths; upload returns `{id,name,file_url}`; HEAD/GET file is image content **from Field API host**, not a NetBox redirect the phone must follow unauthenticated.

- [ ] **Step 6: Commit**

```powershell
git add field_portal/field_api.py
git commit -m "feat: proxy device image list, file, and upload via field API"
```

---

### Task 4: Portal UI — button + modal

**Files:**
- Modify: `field_portal/index.html`

- [ ] **Step 1: Add CSS for image button + image modal**

Near existing `.modal` / `.cable-title` styles, add:

```css
.cable-title-row { display:flex; align-items:flex-start; gap:8px; flex-wrap:wrap; }
.cable-title-row .cable-title { flex:1; min-width:0; margin-bottom:0; }
.img-btn {
  flex:0 0 auto; border:1px solid var(--border); background:#15202b; color:#90caf9;
  border-radius:8px; padding:4px 10px; font-size:.72rem; font-weight:600;
  min-height:32px; cursor:pointer; touch-action:manipulation;
}
.img-btn:active { background:#1a2836; }
.img-modal-bg { z-index:120; }
.img-grid {
  display:grid; grid-template-columns:repeat(auto-fill,minmax(96px,1fr)); gap:10px;
  margin:8px 0 14px;
}
.img-thumb {
  aspect-ratio:1; border-radius:10px; border:1px solid var(--border);
  background:#0d1218; overflow:hidden; cursor:pointer; padding:0;
}
.img-thumb img { width:100%; height:100%; object-fit:cover; display:block; }
.img-empty { color:var(--muted); font-size:.8rem; line-height:1.45; padding:12px 0; }
.img-preview {
  display:none; margin:0 0 12px; border-radius:12px; overflow:hidden;
  border:1px solid var(--border); background:#000; text-align:center;
}
.img-preview.open { display:block; }
.img-preview img { max-width:100%; max-height:50vh; object-fit:contain; vertical-align:middle; }
.img-upload-row { display:flex; flex-direction:column; gap:8px; margin-top:8px; }
.img-upload-row input[type=file] { font-size:.8rem; color:var(--muted); }
```

- [ ] **Step 2: Add modal HTML**

After the existing `#modalBg` block, add:

```html
<div class="modal-bg img-modal-bg" id="imgModalBg">
  <div class="modal">
    <div class="modal-handle"></div>
    <div class="modal-head">
      <button type="button" class="close-x" id="imgModalClose" aria-label="关闭">&times;</button>
      <h3 id="imgModalTitle">设备图片</h3>
      <div class="sub" id="imgModalSub">查看或上传现场照片</div>
    </div>
    <div id="imgPreview" class="img-preview"><img id="imgPreviewEl" alt="预览"></div>
    <div id="imgGrid" class="img-grid"></div>
    <div id="imgEmpty" class="img-empty" style="display:none">暂无图片，可在下方上传</div>
    <div class="img-upload-row">
      <input type="file" id="imgFile" accept="image/*" capture="environment">
      <button type="button" class="btn" id="imgUploadBtn">上传图片</button>
    </div>
    <div id="imgToast" class="toast"></div>
  </div>
</div>
```

- [ ] **Step 3: Wire render + JS**

In `renderRoomPanel`, change the title line to include the button (use `device_id`; hide button if missing):

```javascript
const did = op.device_id || 0;
const imgBtn = did
  ? `<button type="button" class="img-btn" data-device-id="${did}" data-title="${esc(title)}" onclick="event.stopPropagation()">图片</button>`
  : '';
// ...
<div class="cable-title-row"><div class="cable-title">${esc(title)}${connBadge}</div>${imgBtn}</div>
```

After `bindOdfSort()` / when rendering completes, bind:

```javascript
document.querySelectorAll('.img-btn').forEach(btn => {
  btn.addEventListener('click', e => {
    e.preventDefault();
    e.stopPropagation();
    openImgModal(Number(btn.dataset.deviceId), btn.dataset.title || '');
  });
});
```

In `bindOdfSort` `pointerdown`, skip if target is `.img-btn` (in addition to `a`):

```javascript
if (e.target.closest('a, .img-btn')) return;
```

Implement:

```javascript
let imgDeviceId = 0;

function imgApiPath(path) {
  // list returns /api/... — prefix with /odf when portal is under /odf
  if ((location.pathname || '').startsWith('/odf') && path.startsWith('/api/')) {
    return '/odf' + path;
  }
  return path;
}

async function openImgModal(deviceId, title) {
  imgDeviceId = deviceId;
  $('imgModalTitle').textContent = title || ('设备 #' + deviceId);
  $('imgPreview').classList.remove('open');
  $('imgModalBg').classList.add('open');
  $('imgToast').className = 'toast loading';
  $('imgToast').textContent = '加载图片…';
  await refreshImgGrid();
}

async function refreshImgGrid() {
  try {
    const data = await apiGet('/device-images?device_id=' + encodeURIComponent(imgDeviceId));
    const grid = $('imgGrid');
    grid.innerHTML = '';
    const results = data.results || [];
    $('imgEmpty').style.display = results.length ? 'none' : 'block';
    results.forEach(it => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'img-thumb';
      const src = imgApiPath(it.file_url);
      b.innerHTML = '<img alt="" loading="lazy" src="' + src + '">';
      b.onclick = () => {
        $('imgPreviewEl').src = src;
        $('imgPreview').classList.add('open');
      };
      grid.appendChild(b);
    });
    $('imgToast').className = 'toast';
    $('imgToast').textContent = '';
  } catch (e) {
    $('imgToast').className = 'toast err';
    $('imgToast').textContent = '加载失败：' + (e.message || e);
  }
}

function closeImgModal() {
  $('imgModalBg').classList.remove('open');
  $('imgPreview').classList.remove('open');
  $('imgPreviewEl').src = '';
  $('imgFile').value = '';
}

$('imgModalClose').onclick = closeImgModal;
$('imgModalBg').onclick = e => { if (e.target === $('imgModalBg')) closeImgModal(); };

$('imgUploadBtn').onclick = async () => {
  const f = $('imgFile').files && $('imgFile').files[0];
  if (!f) {
    $('imgToast').className = 'toast err';
    $('imgToast').textContent = '请先选择图片';
    return;
  }
  $('imgToast').className = 'toast loading';
  $('imgToast').textContent = '上传中…';
  $('imgUploadBtn').disabled = true;
  try {
    await uploadDeviceImage(imgDeviceId, f);
    $('imgToast').className = 'toast ok';
    $('imgToast').textContent = '上传成功';
    $('imgFile').value = '';
    await refreshImgGrid();
  } catch (e) {
    $('imgToast').className = 'toast err';
    $('imgToast').textContent = '上传失败：' + (e.message || e);
  } finally {
    $('imgUploadBtn').disabled = false;
  }
};
```

Then define `uploadDeviceImage` / `bytesToBase64` as below (canonical upload path — do not keep a separate FormData-only handler).

Use existing `apiGet` for list (`apiGet('/device-images?device_id=...')`) — there is **no** `getJson` in `index.html`.

Upload helper (JSON under `/odf` for Node-RED; multipart on direct `/api`):

```javascript
function bytesToBase64(u8) {
  let s = '';
  const chunk = 0x8000;
  for (let i = 0; i < u8.length; i += chunk) {
    s += String.fromCharCode.apply(null, u8.subarray(i, i + chunk));
  }
  return btoa(s);
}

async function uploadDeviceImage(deviceId, file) {
  if (api.startsWith('/odf')) {
    const u8 = new Uint8Array(await file.arrayBuffer());
    const r = await fetch(api + '/device-images', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        device_id: deviceId,
        name: file.name,
        filename: file.name,
        content_base64: bytesToBase64(u8),
      }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    return data;
  }
  const fd = new FormData();
  fd.append('device_id', String(deviceId));
  fd.append('image', file, file.name);
  fd.append('name', file.name);
  const r = await fetch(api + '/device-images', { method: 'POST', body: fd });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}
```

After deploy / code change, load room panel with refresh (`refreshPanel` or `?refresh=1`) so cached payloads without `device_id` are not used.

- [ ] **Step 4: Manual UI check**

1. Open room panel → **图片** next to route name  
2. Modal lists images; thumbnails load from Field API (`/api/device-images/.../file` or `/odf/api/...`)  
3. Upload phone photo → appears without full reload  
4. Drag-sort still works; clicking **图片** does not start drag  
5. DevTools: image requests must **not** hit NetBox `/media/` from the phone  

- [ ] **Step 5: Commit**

```powershell
git add field_portal/index.html
git commit -m "feat: add room-panel image attachment modal"
```

---

### Task 5: Node-RED proxy for upload + binary file

**Files:**
- Modify: `field_portal/nodered_flow.json`

**Why:** Existing GET uses `"ret": "obj"` (JSON parse) — binary file GETs break. Existing POST only allows `/submit` and `/room-order`.

- [ ] **Step 1: Branch binary `/file` GETs inside the existing catch-all (required)**

Do **not** rely on a second `http in` plus `return null` — Express/Node-RED uses first-match; existing `/api/:path(*)` already owns `device-images/7/file`.

Change both catch-all GET functions (`转发 /api GET` and `转发 /odf/api GET`) to detect `/file` and set a flag, then use a **Switch** node:

- If `msg.isImageFile === true` → dedicated `http request` with `"ret": "bin"` → header-fix function → `http response`
- Else → existing `odf_http_req` (`ret: obj`) → `odf_http_res`

Catch-all function body (pattern):

```javascript
const cfg = global.get('fieldPortal') || flow.get('fieldPortal') || {};
const base = cfg.field_api_url || 'http://127.0.0.1:8765';
const p = msg.req.params.path || 'tree';
const q = msg.req.query || {};
const qs = Object.keys(q).map(k => k + '=' + encodeURIComponent(q[k])).join('&');
msg.url = base + '/api/' + p + (qs ? '?' + qs : '');
msg.method = 'GET';
msg.isImageFile = String(p).endsWith('/file');
msg.headers = {};
return msg;
```

After binary http request, normalize headers:

```javascript
msg.statusCode = msg.statusCode || 200;
const ct = (msg.headers && (msg.headers['content-type'] || msg.headers['Content-Type'])) || 'application/octet-stream';
msg.headers = { 'Content-Type': ct, 'Cache-Control': 'private, max-age=60' };
return msg;
```

Optional: dedicated `/api/device-images/:id/file` http-in nodes are **not required** if the Switch branch above is wired; omit them to avoid route confusion.

- [ ] **Step 2: Add JSON POST proxy for uploads (Node-RED)**

Field API already accepts multipart (direct `:8765`) and JSON `content_base64` (Task 3). Frontend under `/odf` posts JSON (Task 4).

Add Node-RED POST nodes mirroring `room-order` (JSON only, `upload: false`):

- `POST /api/device-images` → forward to `http://127.0.0.1:8765/api/device-images`
- `POST /odf/api/device-images` → same

Function body same pattern as room-order (`Content-Type: application/json`, `msg.payload` pass-through).

- [ ] **Step 3: Verify list vs file paths**

- List: `GET /api/device-images?device_id=` → JSON branch (`ret: obj`) — unchanged  
- File: `GET /api/device-images/7/file` → binary branch (`ret: bin`) — must return image bytes  

Do **not** use `return null` for `/file` in the catch-all.

- [ ] **Step 4: Deploy note**

After editing `nodered_flow.json`, re-import / Deploy the flow on the server (manual ops step). Local curl against `:8765` works without NR.

- [ ] **Step 5: Commit**

```powershell
git add field_portal/nodered_flow.json
git commit -m "feat: proxy device image files and uploads through Node-RED"
```
---

### Task 6: End-to-end verification checklist

- [ ] **Step 1: Run unit tests**

```powershell
cd C:\Users\Administrator\netbox-fiber-import\field_portal
python test_field_images.py -v
```

- [ ] **Step 2: Manual checklist (from spec)**

1. Room panel shows **图片** beside route name  
2. Modal loads empty or existing thumbnails  
3. Upload JPG/PNG → grid refreshes; image appears in NetBox device attachments  
4. Network tab: image `src` is Field API / Node-RED path, not NetBox `/media/`  
5. ODF drag-sort unaffected  
6. Port modal + NetBox link still work  

- [ ] **Step 3: Final commit only if leftover fixes**

```powershell
git status
# commit any remaining fixes with a clear message
```

---

## Out of scope (do not implement)

- Delete attachment UI  
- Pre-fetch “has images” badge on room panel  
- Port-modal image button  
- NetBox / FMS plugin changes  

---

## Execution handoff

After this plan is approved by the plan reviewer and the user picks an execution mode:

1. **Subagent-Driven (recommended)** — `superpowers:subagent-driven-development`  
2. **Inline Execution** — `superpowers:executing-plans`
