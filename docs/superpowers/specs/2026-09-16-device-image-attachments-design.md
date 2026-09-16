# Device Image Attachments — Design

**Date:** 2026-09-16  
**Status:** Approved for planning  
**Scope:** Field Portal room panel — view and upload NetBox device image attachments

## Goal

On the ODF Field Portal room panel, add a small **图片** button next to each route display name (`.cable-title`). Clicking it opens a styled modal where field users can:

1. View existing NetBox image attachments for that ODF device
2. Upload new images

Reuse NetBox’s built-in ImageAttachment model/API. Do not modify NetBox core or the FMS plugin.

## Non-goals

- Delete / replace-only single image workflow
- Embedding or iframe of NetBox UI
- Image attachment controls on the port detail modal
- Changing cable provisioning or auto-connect behavior
- Auth changes (continue using Field API’s existing NetBox token)

## Approach

**Field API proxy (chosen).** Browser talks only to Field API for metadata, upload, **and image bytes**. Field API uses the existing NetBox token to call NetBox ImageAttachment APIs and to fetch media files. The phone must not depend on direct NetBox `/media/` URLs (often unreachable or auth-walled from the field network).

Rejected alternatives:

- Deep-link / iframe NetBox forms — requires NetBox browser login; poor mobile UX
- Link-only to device page — no in-portal modal
- Returning absolute NetBox media URLs to the browser — breaks the “portal-only” access model

## UX

### Entry point

- Location: room panel title row — sibling of `.cable-title` / FC·SC badge inside the flex title area (not inside the drag handle)
- Control: compact **图片** button next to the name/badge
- v1: no “has images” count badge / pre-fetch indicator (keep list+upload only)

### Modal

- Overlay + card, matching existing portal dark theme (`.modal-bg` / rounded card patterns)
- Header: device / route display name + close
- Body: thumbnail grid of attachments; tap thumbnail → larger preview
- Thumbnails and full preview load via Field API image proxy URLs (same origin as the portal), not NetBox hosts
- Empty state: short hint that no images exist yet
- Footer: file picker + **上传** button
- After successful upload: refresh the grid; show brief success/error toast in-modal
- Drag-sort of ODF blocks must ignore clicks on this button (same pattern as existing NetBox link `stopPropagation`; also skip in `pointerdown` on `.odf-block-head`)

### Constraints

- Mobile-first (phones used on site)
- Multiple images per device allowed (NetBox default)
- No delete UI in this release

## Backend

### Panel payload

In `field_browser.py` route/panel builders that already expose `device_url`, also expose:

```json
"device_id": <int>
```

Frontend uses `device_id` for API calls; keep `device_url` unchanged for the existing NetBox link.

### Field API endpoints

| Method | Path | Behavior |
|--------|------|----------|
| `GET` | `/api/device-images?device_id=` | Proxy list: NetBox `GET /api/extras/image-attachments/?object_type=dcim.device&object_id={id}` |
| `GET` | `/api/device-images/{attachment_id}/file` | Proxy image bytes: Field API fetches the NetBox media URL server-side (with token / session as required) and streams the response (`Content-Type` preserved) |
| `POST` | `/api/device-images` | Proxy create: multipart `device_id` + `image` (+ optional `name`); NetBox `POST /api/extras/image-attachments/` with `object_type=dcim.device`, `object_id`, `image` |

Response shape for list (normalize for the portal):

```json
{
  "device_id": 123,
  "count": 2,
  "results": [
    {
      "id": 1,
      "name": "...",
      "file_url": "/api/device-images/1/file"
    }
  ]
}
```

`file_url` is always a Field API path (relative to the portal origin / Node-RED prefix). Frontend uses it for both thumbnail `img src` and full-size preview. Do not expose raw NetBox `/media/` URLs to the browser.

Errors: return JSON `{ "error": "..." }` with appropriate HTTP status for list/upload; for file proxy, return the upstream status or 502 on fetch failure. Do not leak the NetBox token.

### Auth / security

- Same as other Field API routes: no NetBox token in the browser
- Validate `device_id` is a positive integer
- Rely on existing portal network posture (Node-RED reverse proxy; Field API localhost)

## Frontend changes

**File:** `field_portal/index.html`

- CSS for image button + image modal (reuse portal variables)
- In `renderRoomPanel()`, render the button with `data-device-id` / display name
- JS: open modal → `GET /api/device-images` → render grid; upload via `FormData` `POST`
- Ensure pointer/drag handlers on `.odf-block-head` skip the new button

## Files to touch

| File | Change |
|------|--------|
| `field_portal/field_browser.py` | Add `device_id` to room/route panel payloads |
| `field_portal/field_api.py` | Add GET list, GET file proxy, POST upload for `/api/device-images` |
| `field_portal/index.html` | Button, modal UI, fetch/upload logic |

Optional helper module only if `field_api.py` grows too large; prefer keeping thin proxies in `field_api.py` or a small function in `field_browser.py` / `field_service.py`.

## Error handling

| Case | Behavior |
|------|----------|
| Missing / invalid `device_id` | 400 + message |
| NetBox list/upload failure | Surface NetBox/status message in modal toast |
| Empty list | Empty-state copy, upload still available |
| Oversized / rejected file | Show NetBox/API error; no silent fail |

## Testing (manual)

1. Open a room panel with known ODF devices
2. Click **图片** → empty or existing thumbnails load
3. Upload a JPG/PNG → appears in grid without full page reload
4. Confirm image also visible on NetBox device Image Attachments
5. Confirm ODF drag-sort still works; button click does not start drag
6. Confirm existing port modal and NetBox link still work

## Success criteria

- Field user can view and upload device photos from the room panel without logging into NetBox in the browser
- Upload persists as NetBox `ImageAttachment` on `dcim.device`
- UI fits existing portal look; modal usable on phone
