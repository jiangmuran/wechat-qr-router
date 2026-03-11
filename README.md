# 微信群二维码路由系统

基于 Flask + SQLite 的群二维码管理服务，支持分组、访问次数统计、动态图片链接、邀请页与 Server 酱通知。

## 功能

- TOTP 登录后台
- 群组管理（群名、注意事项）
- 上传二维码图片并自动解析，再生成统一样式二维码缓存
- 支持粘贴二维码内容或直接粘贴二维码截图
- 动态图片链接（自动 302 到访问次数最少的二维码）
- 无可用二维码时自动使用后备二维码
- 过期/无可用/访问次数提醒（Server 酱）

## 依赖

- Python 3.10+
- 依赖包见 `requirements.txt`

## 安装

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp config.py.example config.py
```

编辑 `config.py`，设置 TOTP 密钥、Server 酱 URL 等。

## 运行

```bash
./.venv/bin/python app.py
```

默认端口：`5002`

## 后台

- 地址：`http://localhost:5002/admin`
- 未登录访问 `/` 与 `/admin` 会返回 `403 forbidden`
- 支持：图片上传、粘贴二维码内容、粘贴二维码截图
- 支持：推送测试
- 支持：在后台删除单个二维码与后备二维码

## 常用链接

- 邀请页面：`/invite/<group_code>`
- 动态图片：`/api/qr-image/<group_code>`（自动 302 到访问次数最少的二维码图片）
- JSON 信息：`/api/qr/<group_code>`（可选）

## 注意

- 上传图片优先自动识别；识别失败时也可直接保存原图作为二维码。
- 访问次数提醒不会禁用二维码，只会通知。
