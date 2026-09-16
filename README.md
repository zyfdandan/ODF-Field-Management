# ODF-Field-Management

面向厂矿 / 园区光纤运维的 ODF（光纤配线架）现场管理系统。当前以 GitHub
`main` 为发布基线（无独立 semver；离线安装包以构建日期戳标识），在已部署的
**NetBox + FMS 光缆插件** 之上提供 Excel 批量导入与手机扫码现场登记：后台表格
与现场表单写入同一套设备 / 端口 / 光路数据，并支持轻量审计、软编辑锁，以及
机房面板上的 **设备图片附件**（查看 / 上传 / 替换 / 删除，复用 NetBox
ImageAttachment）。

> 本仓库**不包含**真实服务器地址、API Token、SSH 密码或厂区拓扑。部署前请按
> 下方「本地私有配置」自行准备。Field API 应仅本机监听，由 Node-RED 对外反代。

## 功能概览

| 能力 | 当前实现 |
|---|---|
| Excel 光路导入 | 光路分段、ODF 内熔接、缆段纤芯 OTDR 光损；导入后全链路校验 |
| 模板生成 | `generate_template.py` 生成标准 Excel 工作簿 |
| 基础设施脚本 | `bootstrap_*` / `check_*` / `fix_*`：测试 ODF、端口命名、纤芯对称性等 |
| Field Portal 表单 | 手机扫码填写本端端口与对端信息，写回 NetBox 并刷新追踪 |
| 机房 / ODF 面板 | 树形浏览、房间面板与 ODF 面板 API |
| 设备图片附件 | 路由名旁「图片」：弹窗查看 / 上传 / 替换 / 删除；Field API 代理 NetBox ImageAttachment（手机无需登录 NetBox） |
| 骨干 / trunk 登记 | `trunk_form.html` 与对应 API（按公开仓实现） |
| 软编辑锁 | 按 ODF\|缆\|纤芯加锁，TTL 约数分钟；身份近似客户端 IP + UA，无账号体系 |
| 审计 | JSONL 审计流与 `/audit` 页面；非完整 RBAC |
| 二维码标签 | 按 ODF 生成扫码入口（指向 Node-RED 公网/内网基址） |
| 离线打包与部署 | `deploy/` 下 PowerShell 打包、Linux 安装脚本与 systemd 示例 |

部分能力依赖 NetBox/FMS 插件版本、Node-RED 流程是否已 Deploy，以及现场网络
可达性。仓库不会把未安装插件或未验收的现场拓扑描述为已经可用。

## 支持平台与技术栈

- 语言：Python 3.10+（建议 3.11）；辅助 PowerShell / Bash。
- Field API：标准库 `ThreadingHTTPServer` + 自研 handler（非 Flask/FastAPI）。
- Excel / HTTP / 二维码：`openpyxl`、`requests`、`qrcode[pil]`。
- 前端：静态 HTML（表单、面板、审计页）。
- 公网入口：Node-RED **3.1.9**（典型 `:1880`，反代 `/odf` 与 `/odf/api`）。
- 数据源：NetBox REST API + FMS 纤缆插件。
- 部署：venv、systemd（`netbox-field-api`、`nodered`），可选 SSH 一键推送。

## 架构

```text
┌──────────────────┐     ┌────────────────────┐
│ Excel / 运维脚本  │     │ 手机扫码 Field 表单  │
└────────┬─────────┘     └─────────┬──────────┘
         │                         │
         │              ┌──────────▼──────────┐
         │              │ Node-RED（:1880）    │
         │              │ 静态页 + API 反代    │
         │              └──────────┬──────────┘
         │                         │ 127.0.0.1
         │              ┌──────────▼──────────┐
         └─────────────►                        │ Field API（:8765）   │
                        │ 持有 NetBox Token    │
                        │ 缓存 / 软锁 / 审计   │
                        │ 图片附件代理         │
                        └──────────┬──────────┘
                                   │
                        ┌──────────▼──────────┐
                        │ NetBox + FMS 插件    │
                        │ 设备/端口/光路/追踪  │
                        │ ImageAttachment      │
                        └─────────────────────┘
```

导入脚本与 Field API 共享 NetBox 数据面。对外只应暴露 Node-RED；Token 留在
本机 Field API 进程，不进入前端静态页。图片字节也经 Field API 代理，手机浏览器
不直连 NetBox `/media/`。

## 仓库结构

| 路径 | 用途 |
|---|---|
| `netbox_config.example.json` | NetBox API 配置模板 |
| `import_from_excel.py` | Excel 一键导入入口 |
| `generate_template.py` | 生成 Excel 模板 |
| `bootstrap_*.py` / `check_*.py` / `fix_*.py` | 引导、校验与修复脚本 |
| `fiber_infra.py` / `naming_rules.py` | 共享命名与导入逻辑 |
| `field_portal/` | Field API、前端页、Node-RED 流程、锁与审计、图片附件 |
| `field_portal/field_images.py` | NetBox 设备图片附件 list / upload / file / delete |
| `field_portal/portal_config.example.json` | 门户配置模板 |
| `deploy/` | 打包、安装、systemd 与部署文档（含 AI 部署清单） |
| `docs/superpowers/` | 功能设计与实现计划（如图片附件） |
| `qr_labels/` | 本地二维码输出（默认不入库） |

## 获取源码

```powershell
git clone https://github.com/zyfdandan/ODF-Field-Management.git
Set-Location ODF-Field-Management
```

## 本地私有配置

仓库不跟踪 Token、SSH 密码、真实 URL 与运行时状态。首次使用请：

- `netbox_config.example.json` → 本地 `netbox_config.json`
- `field_portal/portal_config.example.json` → 本地 `field_portal/portal_config.json`
- 如需 SSH 部署：`deploy/deploy.env.example`（若有）→ 本地 `deploy/deploy.env`

下列路径仅本机保留，勿提交：

- `field_portal/edit_locks.json`、缓存 JSON、`logs/`
- `qr_labels/`、`backup/`、`dist/`、真实拓扑报告

真实值只保存在本机或服务器安全路径。不要在 issue、日志或截图中公开它们。

## 运行依赖

1. Python 3.10+，以及可访问的 NetBox（已装 FMS 相关插件）。
2. 现场门户另需 Node.js / Node-RED（可用 `deploy` 离线包）。
3. 安装依赖：

```powershell
pip install requests openpyxl "qrcode[pil]"
```

## 快速开始

### 1. 配置 NetBox

编辑本地 `netbox_config.json`：

| 字段 | 说明 |
|---|---|
| `base_url` | NetBox API，如 `http://你的主机:8000/api` |
| `web_base_url` | NetBox 网页根，如 `http://你的主机:8000` |
| `token` | NetBox API Token（仅本机保存） |

### 2. Excel 导入

```powershell
python generate_template.py
python import_from_excel.py
```

### 3. 启动 Field Portal

```powershell
python field_portal\field_api.py --host 127.0.0.1 --port 8765
```

另开终端启动 Node-RED，导入 `field_portal/nodered_flow.json` 并 Deploy。  
（生产环境由 `install_server.sh` 写入 `.nodered/flows.json` 并重启 `nodered`；
更新流程后需重新拷贝 flow 并 `systemctl restart nodered`。）

生成二维码：

```powershell
python field_portal\generate_odf_qr.py --base-url http://你的NodeRED主机:1880
```

健康检查：

```text
curl http://127.0.0.1:8765/health
curl http://主机:1880/
```

### 4. 设备图片附件（机房面板）

1. 浏览器打开 Node-RED 门户 `/` 或 `/portal`，进入某机房面板。  
2. 每条路由显示名旁点 **图片** → 弹窗中查看缩略图 / 上传 / 替换 / 删除。  
3. 附件写入 NetBox 对应 ODF 设备的 Image Attachments；现场手机无需登录 NetBox。

Field API（经 Node-RED 时前缀为 `/odf/api`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/device-images?device_id=` | 列出设备图片 |
| GET | `/api/device-images/{id}/file` | 代理图片字节（勿直连 NetBox media） |
| POST | `/api/device-images` | 上传（门户经反代时用 JSON `content_base64`；直连可用 multipart） |
| DELETE | `/api/device-images/{id}` | 删除附件 |

Node-RED 须支持：`/file` 走二进制转发、`apiMaxLength` 足够大（安装脚本默认
`25mb`），以及 POST/DELETE `/device-images`。详见 `deploy/AI-DEPLOY.md`。

## 服务器部署

公开仓 `deploy/` 提供打包与安装入口，典型流程：

1. 本机构建离线包（可选包含 Node-RED）。
2. 上传到 Linux 服务器并执行 `deploy/install_from_package.sh`。
3. 启用 systemd 单元，确认 Field API 仅监听本机、Node-RED 对外。

- 人类操作：`deploy/DEPLOYMENT.md`
- **AI / Agent 自动部署清单：`deploy/AI-DEPLOY.md`**

生产环境务必轮换 Token，并限制 Node-RED / API 的网络暴露面。

## 测试与验证

- 导入后：在 NetBox 核对设备、端口、光路与追踪页是否一致。
- 现场：扫码打开表单 → 提交 → 确认锁冲突与审计记录。
- 部署后：本机 `/health` 与门户首页应可打开。
- 图片：机房面板「图片」→ 上传 JPG → NetBox 设备页能看到同一附件；替换 /
  删除后列表与 NetBox 一致；缩略图请求应走 Field API / Node-RED，而非 NetBox
  `/media/`。

## 许可与使用边界

仓库未放置 OSI 开源许可证文件时，默认保留所有权利，仅供获授权场景使用。
请遵守当地法规与业主网络管理规定；勿对外分发真实厂区拓扑、Token 或 SSH
凭据。
