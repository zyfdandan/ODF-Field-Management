# NetBox 光纤现场门户 — 部署安装指南

本文档说明如何在 **新 Linux 服务器** 上从零部署 **ODF 现场光路登记门户**（Field Portal），包含 Node-RED 前端跳板与 Python Field API 后端。

---

## 1. 系统架构

```
手机/浏览器
    ↓ :1880
Node-RED（公网入口，静态页面 + API 代理）
    ↓ 127.0.0.1:8765
Field API（Python，持有 NetBox Token）
    ↓ :8000
NetBox FMS API（光路/端口/跳纤/追踪）
```

| 组件 | 端口 | 说明 |
|------|------|------|
| Node-RED | **1880** | 对外：浏览管理 `/`、扫码表单 `/odf`、API 代理 `/odf/api/*` |
| Field API | **8765** | 仅本机监听，不直接对外 |
| NetBox | **8000** | 需已安装 FMS 纤缆管理插件 |

**服务器目录（默认）：** `/home/zttg/netbox-field-portal`

**systemd 服务：**

- `netbox-field-api.service` — Python API
- `nodered.service` — Node-RED（需 bundled `node-red-local/`）

---

## 2. 前置条件

### 服务器

| 项目 | 要求 |
|------|------|
| OS | Ubuntu 20.04+ / Debian 11+ 等 Linux |
| Python | **3.10+**（含 `python3-venv`） |
| 权限 | 部署用户可 `sudo`（写 systemd 单元） |
| 网络 | 服务器能访问 NetBox API；客户端能访问 **1880** |
| 磁盘 | 约 500MB（含 Node-RED bundle） |

```bash
# Ubuntu/Debian 示例
sudo apt update
sudo apt install -y python3 python3-venv python3-pip curl unzip
```

### 开发机（打安装包 / 远程部署）

| 项目 | 用途 |
|------|------|
| Windows + PowerShell | 运行 `deploy/build_package.ps1`、`deploy/deploy_to_server.ps1` |
| PuTTY（plink/pscp） | SSH/SCP 上传 |
| Node.js LTS + npm | 构建 `node-red-local.tar.gz`（仅开发机，服务器不需要 npm） |

### NetBox

1. NetBox 已运行且 FMS 插件可用  
2. 创建 **API Token**（读写 DCIM + Circuits/FMS）  
3. ODF 设备、缆段、端口已在 NetBox 中（可用 Excel 导入工具批量创建）

---

## 3. 配置文件

部署前准备两个 JSON（**含密钥，勿提交 Git**）：

### `netbox_config.json`（项目根目录）

```json
{
  "base_url": "http://YOUR_NETBOX:8000/api",
  "web_base_url": "http://YOUR_NETBOX:8000",
  "token": "YOUR_NETBOX_API_TOKEN",
  "verify_ssl": false
}
```

可从 `netbox_config.example.json` 复制。

### `field_portal/portal_config.json`

```json
{
  "nodered_base_url": "http://YOUR_SERVER:1880",
  "field_api_url": "http://127.0.0.1:8765",
  "allowed_route_keys": ["..."],
  "route_display_names": { "...": "..." },
  "cache_webhook_secret": ""
}
```

可从 `field_portal/portal_config.example.json` 复制，并修改 `nodered_base_url` 为手机可访问的地址。

---

## 4. 方式 A：生成离线安装包（推荐）

在 **开发机** 项目根目录执行：

```powershell
cd C:\Users\Administrator\netbox-fiber-import

# 1. 构建 Node-RED 离线包（需 Node.js，约 100–200MB）
powershell -ExecutionPolicy Bypass -File deploy\build_nodered_bundle.ps1

# 2. 打完整 release zip（含 Node-RED tar）
powershell -ExecutionPolicy Bypass -File deploy\build_package.ps1 -IncludeNodeRed
```

输出：`dist/netbox-field-portal-YYYYMMDD.zip`

### 在服务器上安装

```bash
# 上传 zip 到服务器
scp dist/netbox-field-portal-*.zip user@server:/tmp/

ssh user@server
cd /tmp
unzip netbox-field-portal-*.zip
cd netbox-field-portal-*

# 放入生产配置（或安装后编辑）
cp /path/to/netbox_config.json .
cp /path/to/portal_config.json field_portal/

# 一键安装（创建 venv、systemd、启动服务）
bash deploy/install_from_package.sh
# 或指定目录：
# APP_DIR=/opt/netbox-field-portal bash deploy/install_from_package.sh
```

---

## 5. 方式 B：Windows 直接远程部署

```powershell
cd C:\Users\Administrator\netbox-fiber-import

# 1. 配置 SSH（复制示例并填写密码/主机）
copy deploy\deploy.env.example deploy\deploy.env
notepad deploy\deploy.env

# 2. 构建 Node-RED 包（首次）
powershell -File deploy\build_nodered_bundle.ps1

# 3. 上传并安装
powershell -ExecutionPolicy Bypass -File deploy\deploy_to_server.ps1
```

`deploy.env` 主要字段：

| 变量 | 说明 |
|------|------|
| `DEPLOY_HOST` | 服务器 IP |
| `DEPLOY_USER` | SSH 用户 |
| `DEPLOY_PASSWORD` | SSH 密码（可留空，脚本会提示输入） |
| `DEPLOY_REMOTE_DIR` | 远程安装路径 |
| `DEPLOY_HOSTKEY` | PuTTY host key fingerprint |

---

## 6. 方式 C：仅更新代码（已装过环境）

```powershell
powershell -File deploy\deploy_to_server.ps1
```

或手动 SCP 核心文件后 SSH 执行：

```bash
APP_DIR=/home/zttg/netbox-field-portal SUDO_PASS='...' bash $APP_DIR/deploy/install_server.sh
```

---

## 7. 验证

```bash
# 本机
curl -s http://127.0.0.1:8765/health
# {"status":"ok"}

systemctl status netbox-field-api
systemctl status nodered

# 经 Node-RED 代理
curl -s http://127.0.0.1:1880/odf/api/health
```

浏览器：

- 管理界面：`http://服务器IP:1880/`
- 扫码表单：`http://服务器IP:1880/odf?odf=设备名`

---

## 8. 运维命令

```bash
# 日志
journalctl -u netbox-field-api -f
journalctl -u nodered -f

# 重启
sudo systemctl restart netbox-field-api nodered

# 端口占用
ss -tlnp | grep -E '8765|1880'
```

### 批量修正 ODF 端口命名（F13 → ZGtoDF2-1 等）

```bash
cd /home/zttg/netbox-field-portal
./venv/bin/python3 fix_odf_port_names.py "轧钢机房至得丰机房"
```

---

## 9. 防火墙

```bash
# ufw 示例：仅开放 Node-RED
sudo ufw allow 1880/tcp
# NetBox 8000 按现有策略；8765 不应暴露公网
```

---

## 10. 与 Excel 导入工具的关系

| 模块 | 位置 | 部署到 Field 服务器？ |
|------|------|----------------------|
| Field Portal | `field_portal/` | **是** |
| Excel 导入 | `import_from_excel.py` 等 | 可选（服务器仅需 `fiber_infra` 等共享模块） |
| 基础设施 bootstrap | `bootstrap_*.py` | 在 NetBox 侧运行，非 Field 服务器必需 |

Excel 批量导入说明见项目根 `README.md`（光路录入 / 熔接 / 光损三表）。

---

## 11. Trace 追踪页 404

若 NetBox 光路追踪页一直转圈，需单独部署 FMS 前端静态文件（与 Field Portal 无关）：

```powershell
python check_trace_health.py
# 参考 netbox-docker-patches 仓库的 deploy_fms_static.sh
```

---

## 12. 故障排查

| 现象 | 处理 |
|------|------|
| `ModuleNotFoundError: openpyxl` | 重新运行 `install_server.sh` 或 `venv/bin/pip install -r requirements.txt` |
| 1880 无法访问 | `systemctl status nodered`；确认 `node-red-local/` 存在 |
| API 502 | `journalctl -u netbox-field-api`；检查 `netbox_config.json` token |
| 端口显示 F13 | 运行 `fix_odf_port_names.py`；Portal 点 ↻ 同步 |
| 机房面板加载慢 | 不要用 `refresh=1` 作为默认加载；用界面 ↻ 手动同步 |

---

## 13. 文件清单

完整清单见 `deploy/MANIFEST.txt`。

**AI 辅助部署** 请阅读同目录 `AI-DEPLOY.md`（给 Cursor / Copilot 等 Agent 的分步指令）。
