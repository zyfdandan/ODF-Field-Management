# AI 部署指令（给 Cursor / Agent 使用）

> 人类用户请阅读 [DEPLOYMENT.md](./DEPLOYMENT.md)。  
> 本文档为 **结构化 checklist**，便于 AI 在新服务器或更新发布时自动完成部署与验收。

---

## 任务目标

在目标 Linux 服务器部署或更新 **NetBox ODF Field Portal**：

| 组件 | 监听 | 说明 |
|------|------|------|
| Field API | `127.0.0.1:8765` | 持有 NetBox Token；含图片附件代理 |
| Node-RED | `0.0.0.0:1880` | 公网入口；静态页 + API / 图片二进制反代 |
| NetBox + FMS | 通常 `:8000` | DCIM + ImageAttachment + FMS |

**功能范围（部署后应可用）：** 机房树与端口面板、扫码登记、trunk、软锁、审计、  
**设备图片附件**（路由名旁「图片」：查看 / 上传 / 替换 / 删除）。

---

## 输入信息（向用户确认）

| 变量 | 示例 | 必填 |
|------|------|------|
| `SERVER_IP` | `192.168.1.10` | 是 |
| `SSH_USER` | `deploy` | 是 |
| `SSH_PASSWORD` 或密钥 | — | 是 |
| `APP_DIR` | `/opt/netbox-field-portal` | 否 |
| `NETBOX_URL` | `http://192.168.1.10:8000` | 是 |
| `NETBOX_TOKEN` | `nbt_...` | 是 |
| `NODERED_PUBLIC_URL` | `http://192.168.1.10:1880` | 是 |

**禁止**把 Token / SSH 密码写入仓库或提交 Git。

---

## 路径约定（代码仓库）

```text
ODF-Field-Management/
├── deploy/
│   ├── AI-DEPLOY.md               # 本文
│   ├── DEPLOYMENT.md              # 人类完整指南
│   ├── build_package.ps1
│   ├── build_nodered_bundle.ps1
│   ├── deploy_to_server.ps1
│   ├── install_server.sh
│   ├── install_from_package.sh
│   ├── requirements.txt
│   └── deploy.env.example
├── field_portal/
│   ├── field_api.py               # HTTP API（含 /api/device-images*）
│   ├── field_images.py            # NetBox ImageAttachment 助手
│   ├── index.html                 # 机房面板 + 图片弹窗
│   ├── nodered_flow.json          # 含二进制 /file 与 DELETE 代理
│   └── …
├── netbox_config.json             # 密钥（gitignore）
└── docs/superpowers/              # 功能设计 / 计划（含图片附件）
```

---

## 流程 A：Windows 开发机远程部署

```powershell
cd <仓库根目录>

# Step 1 — 配置
# copy netbox_config.example.json netbox_config.json 并填入 token
# copy field_portal\portal_config.example.json field_portal\portal_config.json

# Step 2 — Node-RED bundle（服务器无 npm）
powershell -ExecutionPolicy Bypass -File deploy\build_nodered_bundle.ps1

# Step 3 — deploy.env
copy deploy\deploy.env.example deploy\deploy.env
# 编辑 DEPLOY_HOST, DEPLOY_USER, DEPLOY_PASSWORD, DEPLOY_HOSTKEY

# Step 4 — 部署
powershell -ExecutionPolicy Bypass -File deploy\deploy_to_server.ps1
```

**成功标准：**

```bash
curl -sf http://127.0.0.1:8765/health   # 在服务器上
curl -sf -o /dev/null -w '%{http_code}\n' http://SERVER_IP:1880/portal
```

---

## 流程 B：离线 zip 包部署

```powershell
powershell -File deploy\build_nodered_bundle.ps1
powershell -File deploy\build_package.ps1 -IncludeNodeRed
```

```bash
unzip netbox-field-portal-*.zip -d /tmp
cd /tmp/netbox-field-portal-*
# 写入 netbox_config.json 与 field_portal/portal_config.json
export APP_DIR=/home/USER/netbox-field-portal
export SUDO_PASS='...'
bash deploy/install_from_package.sh
```

---

## 流程 C：仅更新代码（已有 systemd）

适用于已部署环境（含本次「图片附件」发布）：

1. 上传至少这些文件到 `$APP_DIR`：
   - `field_portal/field_api.py`
   - `field_portal/field_images.py`（新文件）
   - `field_portal/field_browser.py`
   - `field_portal/index.html`
   - `field_portal/nodered_flow.json`
   - `field_portal/install_server.sh`（可选，用于同步 `apiMaxLength`）
2. **合并** Node-RED 流程：不要用仅含 ODF 基础节点的 flow 直接覆盖已带
   trunk / audit 的线上 `flows.json`。正确做法：
   - 以线上 `.nodered/flows.json` 为底；
   - 用仓库 `nodered_flow.json` 中的节点 **按 id 覆盖/新增**（含
     `odf_sw_img_file`、`odf_http_req_bin`、device-images POST/DELETE）；
   - 保留线上独有节点（如 `/trunk`、`audit/beacon`）。
3. 确认 `.nodered/settings.js` 含：

```js
apiMaxLength: '25mb',
```

4. 重启服务（优先 systemd，避免与手工进程抢 8765）：

```bash
sudo systemctl reset-failed netbox-field-api
sudo systemctl restart netbox-field-api
sudo systemctl restart nodered
sleep 2
curl -sf http://127.0.0.1:8765/health
systemctl is-active netbox-field-api nodered
```

5. 机房面板若仍无 `device_id` / 「图片」按钮：对 room-panel 带 `refresh=1`
   或调用 `invalidate_room_panel_cache()`，并让浏览器 **强制刷新**。

---

## install_server.sh 会自动完成

1. 删除误部署在根目录的 `field_service.py`
2. 从 example 复制缺失配置（并 WARN）
3. `python3 -m venv venv` + `pip install -r requirements.txt`
4. 解压/检测 `node-red-local/`
5. 写入 `.nodered/flows.json` + `settings.js`（含 `apiMaxLength: '25mb'`）
6. 创建并启动 `netbox-field-api.service`、`nodered.service`

| 变量 | 默认 |
|------|------|
| `APP_DIR` | `/home/zttg/netbox-field-portal` |
| `APP_USER` | `zttg` |
| `NODERED_PORT` | `1880` |
| `SUDO_PASS` | 空 |

---

## 部署后验证 checklist

### 基础

- [ ] `systemctl is-active netbox-field-api` → `active`
- [ ] `systemctl is-active nodered` → `active`
- [ ] `curl http://127.0.0.1:8765/health` → `{"status":"ok"}`
- [ ] 浏览器 `http://SERVER_IP:1880/` 有机房树与端口网格
- [ ] `netbox_config.json` token 有效（无 401）

### 设备图片附件

- [ ] 服务器存在 `field_portal/field_images.py`
- [ ] `curl -s 'http://127.0.0.1:8765/api/device-images?device_id=0'` 返回 JSON 错误（说明路由已挂载），而非 404
- [ ] Node-RED flow 含 `isImageFile` / `odf_http_req_bin` / `device-images` POST 与 DELETE
- [ ] `.nodered/settings.js` 含 `apiMaxLength: '25mb'`
- [ ] 门户机房面板路由名旁有 **图片** 按钮
- [ ] 弹窗可上传 JPG；`GET .../device-images?device_id=<真实ID>` 有结果
- [ ] 缩略图 URL 为 Field API / `/odf/api/.../file`，**不是** NetBox `/media/`
- [ ] 替换、删除后列表与 NetBox 设备 Image Attachments 一致
- [ ] 手机大图（数 MB）上传不因 `PayloadTooLargeError` 失败

**冒烟命令示例（在服务器上）：**

```bash
# 列表（把 DEVICE_ID 换成真实 ODF 设备 id）
curl -s "http://127.0.0.1:8765/api/device-images?device_id=DEVICE_ID" | head -c 400

# 经 Node-RED 上传需 JSON base64（勿对 :1880 直接 multipart）
# 删除：
curl -s -X DELETE "http://127.0.0.1:8765/api/device-images/ATTACHMENT_ID"
curl -s -X DELETE "http://127.0.0.1:1880/api/device-images/ATTACHMENT_ID"
```

---

## 常见问题修复

### openpyxl / requests 缺失

```bash
$APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt
sudo systemctl restart netbox-field-api
```

### Field API 起不来 / Address already in use

```bash
# 勿同时手工 Popen 与 systemd
sudo systemctl stop netbox-field-api
fuser -k 8765/tcp 2>/dev/null || true
pkill -f 'field_portal/field_api.py' 2>/dev/null || true
sudo systemctl reset-failed netbox-field-api
sudo systemctl start netbox-field-api
```

### 上传 UTF-8 / `0xff` 或 NetBox JSON parse error

- 经 Node-RED 的门户必须用 **JSON `content_base64`** 上传（前端已如此）。
- Field API 调 NetBox multipart 时须去掉 session 的 `Content-Type: application/json`
  （见 `field_images.upload_device_image`）。

### 上传 HTTP 500 / PayloadTooLargeError

提高 `.nodered/settings.js` 的 `apiMaxLength`（建议 `25mb`），重启 nodered。

### 图片缩略图裂图 / Node-RED JSON parse error on GET

`/api/.../file` 必须走 **`ret: "bin"`** 分支，不能走默认 `ret: "obj"` 的 http request。

### 无「图片」按钮

面板 JSON 缺 `device_id` → 确认已部署新 `field_browser.py`，并 `refresh=1` / 清
room-panel 缓存；浏览器 Ctrl+F5。

### Node-RED 未启动

```bash
ls -la $APP_DIR/node-red-local/node_modules/.bin/node-red
bash $APP_DIR/deploy/install_server.sh
```

---

## 不要做的事

- 不要将 `netbox_config.json`、`deploy.env`、PAT / SSH 密码提交 Git 或贴进公开 issue
- 不要将 Field API `8765` 暴露公网
- 不要用「仅基础 ODF 节点」的 `nodered_flow.json` 整文件覆盖已有 trunk/审计流程
- 不要在生产使用 example 占位 Token
- 不要随意删除 `field_portal/circuit_store.json`（未完成光路段会丢）

---

## 相关文档

- [DEPLOYMENT.md](./DEPLOYMENT.md) — 完整安装指南  
- [../field_portal/README.md](../field_portal/README.md) — 门户功能说明  
- [../README.md](../README.md) — 仓库总览与图片附件 API  
- [../docs/superpowers/specs/2026-09-16-device-image-attachments-design.md](../docs/superpowers/specs/2026-09-16-device-image-attachments-design.md) — 图片附件设计  
