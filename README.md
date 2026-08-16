# ODF 现场管理系统

基于 **NetBox + FMS 光缆插件** 的光纤配线架（ODF）现场作业系统：  
支持 Excel 批量导入光路/熔接/光损，以及手机扫码现场登记（Field Portal）。

> 本仓库**不包含**真实服务器地址、API Token、SSH 密码等敏感信息。部署前请按下方说明自行配置。

---

## 项目简介

面向厂矿/园区光纤网络运维场景，把「表格录入」和「现场扫码」接到同一套 NetBox 数据上：

- **后台导入**：用 Excel 维护光路分段、ODF 内熔接、缆段纤芯 OTDR 光损  
- **现场门户**：每个 ODF 贴二维码，手机扫码填写本端端口与对端信息，自动写回 NetBox 并刷新追踪  
- **部署工具**：提供离线安装包脚本与 SSH 一键部署（需自备 Linux 服务器与 NetBox）

---

## 主要能力

| 模块 | 能力 |
|------|------|
| Excel 导入 | 光路录入、熔接录入、缆段纤芯光损；导入后全链路校验 |
| 基础设施脚本 | 引导创建测试 ODF/缆段、端口命名同步、纤芯对称性检查等 |
| Field Portal | 移动端表单、机房面板浏览、编辑锁、审计页 |
| 二维码 | 按 ODF 生成扫码入口（Node-RED 公网/内网地址） |
| 部署 | `deploy/` 下打包、安装、systemd 服务示例 |

---

## 架构概览

```
┌──────────────────┐     ┌────────────────────┐
│  Excel / 脚本导入 │     │ 手机扫码 Field 表单  │
└────────┬─────────┘     └─────────┬──────────┘
         │                         │
         │              ┌──────────▼──────────┐
         │              │ Node-RED（跳板 :1880）│
         │              └──────────┬──────────┘
         │                         │
         │              ┌──────────▼──────────┐
         └─────────────►│ Field API（:8765）   │
                        │ 持有 NetBox Token     │
                        └──────────┬──────────┘
                                   │
                        ┌──────────▼──────────┐
                        │ NetBox + FMS 插件     │
                        │ 设备/端口/光路/追踪   │
                        └─────────────────────┘
```

---

## 目录结构

```
ODF-Field-Management/
├── README.md                 # 本说明
├── netbox_config.example.json
├── import_from_excel.py      # Excel 一键导入
├── generate_template.py      # 生成 Excel 模板
├── bootstrap_*.py            # 测试基础设施
├── field_portal/             # 现场门户（API + 前端页 + Node-RED 流程）
│   ├── field_api.py
│   ├── field_service.py
│   ├── index.html / form.html / audit.html
│   ├── nodered_flow.json
│   └── portal_config.example.json
├── deploy/                   # 安装与部署文档/脚本
└── qr_labels/                # 本地生成二维码（不入库）
```

---

## 环境要求

- Python 3.10+（建议 3.11）
- 可访问的 NetBox 实例，并安装光纤/FMS 相关插件
- 现场门户另需：Node.js / Node-RED（可用 `deploy` 离线包）
- Windows 可用于打安装包与 SSH 部署；服务器端建议 Linux

依赖示例：

```powershell
pip install requests openpyxl qrcode[pil]
```

---

## 快速开始

### 1. 配置 NetBox（必做）

```powershell
copy netbox_config.example.json netbox_config.json
```

编辑 `netbox_config.json`：

| 字段 | 说明 |
|------|------|
| `base_url` | NetBox API，如 `http://你的主机:8000/api` |
| `web_base_url` | NetBox 网页根，如 `http://你的主机:8000` |
| `token` | NetBox API Token（仅本机保存，勿提交） |

### 2. Excel 导入光路

```powershell
# 生成模板
python generate_template.py

# 填写「光路录入 / 熔接录入 / 缆段纤芯光损」后导入
python import_from_excel.py
```

导入成功会提示全链路校验通过；失败时按提示检查熔接或缆段编号。

### 3. 现场门户（扫码登记）

```powershell
copy field_portal\portal_config.example.json field_portal\portal_config.json
# 将 nodered_base_url 改成手机能访问的 Node-RED 地址

# 启动 Field API（持有 Token，勿暴露到公网无鉴权）
python field_portal\field_api.py --host 127.0.0.1 --port 8765

# 另开终端启动 Node-RED，导入 field_portal/nodered_flow.json 并 Deploy
node-red

# 生成 ODF 二维码
python field_portal\generate_odf_qr.py --base-url http://你的NodeRED主机:1880
```

更细的门户说明见 [`field_portal/README.md`](field_portal/README.md)。  
完整服务器部署见 [`deploy/DEPLOYMENT.md`](deploy/DEPLOYMENT.md)。

---

## 安全说明

公开仓库已刻意排除 / 脱敏：

- `netbox_config.json`、`portal_config.json`、`deploy.env`
- SSH 密码、API Token、真实内网地址
- 运行时缓存、现场业务数据导出、本地 backup
- 带真实 URL 的二维码清单与追踪 HTML 快照

**请勿**把生产 Token、密码、厂区真实拓扑机密数据推送到 Git。若 Token 曾泄露，请在 NetBox 中立即轮换。

---

## 相关文档

- [field_portal/README.md](./field_portal/README.md) — 扫码门户  
- [deploy/DEPLOYMENT.md](./deploy/DEPLOYMENT.md) — 人工部署  
- [deploy/AI-DEPLOY.md](./deploy/AI-DEPLOY.md) — 结构化部署清单  

---

## 声明

本项目面向已获授权的网络资源管理场景。使用前请遵守单位制度与当地法律法规，勿用于未授权的网络探测或变更。
