# ODF 扫码现场登记（Field Portal）

在每个 ODF 框贴二维码，现场扫码打开移动端表单，填写光路段或光损，经 **Node-RED 跳板** 转发到本机 **Field API**，自动写入 NetBox 并形成追踪链路。

## 架构

```
手机扫码 → Node-RED :1880/odf?odf=机房A-ODF-01
         → Field API :8765 (Python, 持有 NetBox Token)
         → NetBox FMS API（端口/跳纤/光路/retrace）
```

## 1. 安装依赖

```powershell
cd <本仓库根目录>
pip install qrcode[pil] requests openpyxl

# Node-RED（首次，也可使用 deploy 离线包）
npm install -g --unsafe-perm node-red
```

## 2. 配置

```powershell
copy field_portal\portal_config.example.json field_portal\portal_config.json
# 编辑 nodered_base_url 为手机能访问的地址（如 http://192.168.1.10:1880）
```

NetBox Token 使用上级目录 `netbox_config.json`（**不要**放进表单或 Node-RED 前端）。

## 3. 启动 Field API

```powershell
python field_portal\field_api.py --host 127.0.0.1 --port 8765
```

生产环境建议只监听本机，由 Node-RED 反代；勿把带 Token 的 API 直接暴露公网。

## 4. 启动 Node-RED 并导入流程

```powershell
node-red
```

浏览器打开 `http://127.0.0.1:1880` → 菜单 → **Import** → 选择 `field_portal/nodered_flow.json` → Deploy。

## 5. 生成 ODF 二维码

```powershell
python field_portal\generate_odf_qr.py --base-url http://你的NodeRED主机:1880
```

输出目录：`qr_labels/odf_field/`（默认不入库），打印 PNG 贴到对应 ODF 框。

## 现场操作流程

### 登记一条完整光路（多 ODF 逐站扫码）

1. **起点 ODF** 扫码 → 填光路编号、选对端 ODF、本端/对端纤芯 → 提交  
2. **中间 ODF** 扫码 → 继续填同光路编号的下一段（系统自动合并并 retrace）  
3. **终点** 补全后，在 NetBox 追踪页查看整条路径  

### 机房面板

门户首页可按站点/机房浏览端口占用与路由，便于现场核对空闲纤芯。

路由显示名旁的 **图片** 可查看 / 上传 / 替换 / 删除该 ODF 设备在 NetBox 上的
图片附件（经 Field API 代理，手机无需登录 NetBox）。API 见仓库根目录
`README.md`「设备图片附件」一节。

## 安全注意

- `portal_config.json`、运行缓存、审计数据仅存本机  
- 部署脚本密码请用环境变量或 `deploy/deploy.env`，不要写进仓库  
- 图片上传经 Node-RED 时使用 JSON base64；勿假设 multipart 能穿过默认反代  
