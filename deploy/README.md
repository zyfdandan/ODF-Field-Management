# 部署工具包（Deploy）

本目录包含 **Field Portal 现场光路登记门户** 的完整部署工具、安装脚本与文档。

## 快速开始

### 打离线安装包（推荐保存/迁移）

```powershell
cd C:\Users\Administrator\netbox-fiber-import

# 需 Node.js — 构建 Node-RED 离线包
powershell -File deploy\build_nodered_bundle.ps1

# 生成 dist\netbox-field-portal-YYYYMMDD.zip
powershell -File deploy\build_package.ps1 -IncludeNodeRed
```

### 远程部署到服务器

```powershell
copy deploy\deploy.env.example deploy\deploy.env
# 编辑 DEPLOY_HOST / DEPLOY_USER / DEPLOY_PASSWORD

powershell -File deploy\deploy_to_server.ps1
```

### 服务器上从 zip 安装

```bash
unzip netbox-field-portal-*.zip && cd netbox-field-portal-*
bash deploy/install_from_package.sh
```

## 文档

| 文件 | 读者 |
|------|------|
| [DEPLOYMENT.md](./DEPLOYMENT.md) | 运维 / 人工安装 |
| [AI-DEPLOY.md](./AI-DEPLOY.md) | Cursor / AI Agent 分步部署 |
| [MANIFEST.txt](./MANIFEST.txt) | 安装包文件清单 |

## 目录说明

| 文件 | 作用 |
|------|------|
| `requirements.txt` | 服务器 Python 依赖 |
| `build_nodered_bundle.ps1` | 构建 `field_portal/node-red-local.tar.gz` |
| `build_package.ps1` | 打 release zip |
| `deploy_to_server.ps1` | Windows SSH 上传 + 安装 |
| `install_server.sh` | 服务器：venv + systemd |
| `install_from_package.sh` | 从解压目录一键安装 |
| `deploy.env.example` | SSH 部署参数模板 |

## 相关

- 功能说明：`../field_portal/README.md`
- Excel 导入：`../README.md`
