# AI 部署指令（给 Cursor / Agent 使用）

> 人类用户请阅读 [DEPLOYMENT.md](./DEPLOYMENT.md)。  
> 本文档为 **结构化 checklist**，便于 AI 在新服务器上自动完成部署。

---

## 任务目标

在目标 Linux 服务器部署 **NetBox ODF Field Portal**：

- Field API：`127.0.0.1:8765`
- Node-RED：`0.0.0.0:1880`
- 依赖 NetBox FMS API（通常 `:8000`）

---

## 输入信息（向用户确认）

| 变量 | 示例 | 必填 |
|------|------|------|
| `SERVER_IP` | `192.168.1.10` | 是 |
| `SSH_USER` | `deploy` | 是 |
| `SSH_PASSWORD` 或密钥 | — | 是 |
| `APP_DIR` | `/opt/netbox-field-portal` | 否 |
| `NETBOX_URL` | `http://192.168.1.10:8000` | 是 |
| `NETBOX_TOKEN` | `nbt_...`（你的 Token） | 是 |
| `NODERED_PUBLIC_URL` | `http://192.168.1.10:1880` | 是 |

---

## 路径约定（代码仓库）

```
netbox-fiber-import/
├── deploy/
│   ├── build_package.ps1          # 打 zip 安装包
│   ├── build_nodered_bundle.ps1   # 构建 node-red-local.tar.gz
│   ├── deploy_to_server.ps1       # Windows → SSH 部署
│   ├── install_server.sh          # 服务器：venv + systemd
│   ├── install_from_package.sh    # 服务器：从 zip 安装
│   ├── requirements.txt
│   └── deploy.env.example
├── field_portal/                  # 门户代码
├── netbox_config.json             # 密钥（gitignore）
└── fiber_infra.py, import_from_excel.py, naming_rules.py, ...
```

---

## 流程 A：Windows 开发机远程部署

```powershell
cd C:\Users\Administrator\netbox-fiber-import

# Step 1 — 确保配置文件存在
# 若缺失：copy netbox_config.example.json netbox_config.json 并填入 token
# 若缺失：copy field_portal\portal_config.example.json field_portal\portal_config.json

# Step 2 — 构建 Node-RED bundle（服务器无 npm）
powershell -ExecutionPolicy Bypass -File deploy\build_nodered_bundle.ps1

# Step 3 — 配置 deploy.env
copy deploy\deploy.env.example deploy\deploy.env
# 编辑 DEPLOY_HOST, DEPLOY_USER, DEPLOY_PASSWORD, DEPLOY_HOSTKEY

# Step 4 — 部署
powershell -ExecutionPolicy Bypass -File deploy\deploy_to_server.ps1
```

**成功标准：**

```bash
curl -sf http://SERVER_IP:1880/odf/api/health
# 或 SSH 上：curl -sf http://127.0.0.1:8765/health
```

---

## 流程 B：离线 zip 包部署（无 Windows）

```powershell
# 在开发机打包含 Node-RED 的包
powershell -File deploy\build_nodered_bundle.ps1
powershell -File deploy\build_package.ps1 -IncludeNodeRed
# 产物：dist/netbox-field-portal-YYYYMMDD.zip
```

```bash
# 在服务器
unzip netbox-field-portal-*.zip -d /tmp
cd /tmp/netbox-field-portal-*

# 写入 netbox_config.json 和 field_portal/portal_config.json

export APP_DIR=/home/USER/netbox-field-portal
export SUDO_PASS='...'   # 如需 sudo 密码
bash deploy/install_from_package.sh
```

---

## install_server.sh 会自动完成

1. 删除误部署在根目录的 `field_service.py`
2. 从 example 复制缺失的配置文件（并 WARN）
3. `python3 -m venv venv`
4. `pip install -r requirements.txt`（requests, openpyxl）
5. 解压/检测 `node-red-local/`
6. 写入 `.nodered/flows.json` + `settings.js`
7. 创建并启动 `netbox-field-api.service`、`nodered.service`

**环境变量：**

| 变量 | 默认 |
|------|------|
| `APP_DIR` | `/home/zttg/netbox-field-portal` |
| `APP_USER` | `zttg` |
| `NODERED_PORT` | `1880` |
| `SUDO_PASS` | 空（无密码 sudo 时不需要） |

---

## 部署后验证 checklist

- [ ] `systemctl is-active netbox-field-api` → `active`
- [ ] `systemctl is-active nodered` → `active`（若有 bundle）
- [ ] `curl http://127.0.0.1:8765/health` → `{"status":"ok"}`
- [ ] 浏览器打开 `http://SERVER_IP:1880/` 有机房树
- [ ] 选择 ODF 后端口网格正常（非全部 F13）
- [ ] `netbox_config.json` 中 token 有效（API 不报 401）

---

## 常见问题修复

### openpyxl / requests 缺失

```bash
$APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt
sudo systemctl restart netbox-field-api
```

### Node-RED 未启动

```bash
ls -la $APP_DIR/node-red-local/node_modules/.bin/node-red
# 若无：重新上传 field_portal/node-red-local.tar.gz 并 tar -xzf
bash $APP_DIR/deploy/install_server.sh
```

### 端口名 F13/F14 显示错误

```bash
cd $APP_DIR
./venv/bin/python3 fix_odf_port_names.py "ODF设备全名"
# Portal 界面点 ↻ 同步
```

### 更新代码不重装 venv

```powershell
powershell -File deploy\deploy_to_server.ps1
```

---

## 不要做的事

- 不要将 `netbox_config.json`、`deploy.env` 提交 Git
- 不要将 Field API 8765 暴露公网（应经 Node-RED 1880 代理）
- 不要在生产环境使用 example 里的 placeholder token
- 不要删除服务器上 `field_portal/circuit_store.json`（有待完成光路段时会丢数据）

---

## 相关文档

- [DEPLOYMENT.md](./DEPLOYMENT.md) — 完整安装指南
- [../field_portal/README.md](../field_portal/README.md) — 功能与 API
- [../README.md](../README.md) — Excel 导入与 NetBox 数据
