# CQTBIBOT

逆向 `szxy.cqtbi.edu.cn` 的 OAuth2 流程，从浏览器登录抓包到脱代理自动拿 access_token。

## 文件

| 文件 | 作用 |
|---|---|
| `sso_login.py` | **v3 自动登录工具**（日常使用）：弹窗选账号 → 填验证码 → 拿 token |
| `sso_tool.py` | **v2 抓包工具**（开发用）：内嵌 mitmproxy，浏览器登录时实时高亮登录请求 |
| `sso_common.py` | 共享常量 + token 接口调用 + accounts.json 读写 |
| `accounts.json` | 账号 + token 缓存（运行时生成） |

## 运行

```bash
# 自动登录（日常）
D:\code\private\class\venv\Scripts\python.exe D:\code\private\class\sso_login.py

# 抓包（接口变更时重抓）
D:\code\private\class\venv\Scripts\python.exe D:\code\private\class\sso_tool.py
```

## 接口文档

### 应用凭证

| 项 | 值 |
|---|---|
| `appid` / `client_id` | `sso20240408001` |
| `secret` | `002658359667427995860705420291461` |
| `redirect_uri` | `http://szxy.cqtbi.edu.cn/oauth2/api-docs/access_token.html` |

### 1. 获取授权码（拿 accKey）

```
GET http://szxy.cqtbi.edu.cn/oauth2/v1/auth2orize
    ?client_id=sso20240408001
    &state=callback
    &redirect_uri=<URL>
```

未登录时服务器返回 **302**：

```
Location: http://szxy.cqtbi.edu.cn/oauth2/Login.html?accKey=<NEW>
```

`accKey` 是本次登录会话 key，一次性，验证码和登录请求都绑这个值。

### 2. 获取图形验证码

```
GET http://szxy.cqtbi.edu.cn/oauth2/v1/createVertifyCode?checkId=<accKey>
```

返回 **JPEG** 图片（100×40 像素）。`checkId` 直接传 accKey；想刷新就再 GET 一次。

### 3. 登录（用户名+密码+验证码）

```
POST http://szxy.cqtbi.edu.cn/oauth2/v1/auth2Login
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
X-Requested-With: XMLHttpRequest
Origin:  http://szxy.cqtbi.edu.cn
Referer: http://szxy.cqtbi.edu.cn/oauth2/Login.html?accKey=<accKey>

ucode=<学号>
&upwd=<RSA加密密码 base64>
&accKey=<accKey>
&checkId=<accKey>
&rcode=<验证码4位>
```

**密码加密**：RSA-1024 / PKCS#1 v1.5，公钥硬编码在 `/oauth2/js/Login.js`：

```
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCAllwmit21QS9mQTG3Ry1pYIOJxffUuYJTy
XKOLIYTmQZMvWEKASShMXjHTzogU+oN5LZQX2HCZyP96mkOeXZvgpnjR9xKltr2KndH4R6Sbc
qbrNWX2q9+uagCgz1MUF8jZhDswiMWMYmLds433qXpCXFBcbA4avIEYzsjcjk5yQIDAQAB
```

加密 → 128 字节密文 → base64 → 172 字符（提交时 `=` 会被 URL-encode 成 `%3D`）。

Python 复现见 `sso_login.py:rsa_encrypt`。

**响应** JSON：

```json
{
  "success": true,
  "ticket": "...",
  "redirect_url": "http://szxy.cqtbi.edu.cn/oauth2/api-docs/access_token.html?code=<CODE>&state=callback",
  "msg": ""
}
```

失败时 `success=false`，`msg` 是中文错误（如「验证码错误」「密码错误」）。从 `redirect_url` 的 query 取 `code` 进入下一步。

### 4. 用 code 换 access_token

```
GET http://szxy.cqtbi.edu.cn/oauth2/v1/access_token
    ?client_id=sso20240408001
    &secret=002658359667427995860705420291461
    &code=<CODE>
```

返回：

```json
{
  "access_token": "8c43dce77fc143dd82b49de159d91943",
  "success": true,
  "errcode": "",
  "refresh_token": "",
  "token_type": "example",
  "expires_in": 3600
}
```

`code` 一次性、几十秒内有效；`access_token` 有效期 3600 秒。

## 完整流程时序

```
工具                         szxy.cqtbi.edu.cn
 │  GET auth2orize              │
 ├─────────────────────────────►│
 │◄──── 302 Location: ?accKey=X─┤
 │                              │
 │  GET createVertifyCode?      │
 │      checkId=X               │
 ├─────────────────────────────►│
 │◄──── JPEG 验证码 ────────────┤
 │                              │
 │  (用户肉眼读验证码 rcode)    │
 │  (RSA 加密密码 upwd)         │
 │                              │
 │  POST auth2Login             │
 │      ucode/upwd/accKey/      │
 │      checkId/rcode           │
 ├─────────────────────────────►│
 │◄── {success:true, redirect_url:".../access_token.html?code=Y"}
 │                              │
 │  GET access_token?           │
 │      client_id/secret/code=Y │
 ├─────────────────────────────►│
 │◄── {access_token:"...", expires_in:3600}
```

## 关键坑

- **accKey 一次性**：登录成功或失败后都作废，必须重走第 1 步拿新的
- **`checkId` 不是独立参数**：直接复用 accKey 即可（JS 源码确认）
- **不需要 cookie**：整个流程靠 accKey 串联；`PORTAL_TICKET` cookie 是登录后才设的，业务侧用
- **登录页文件名是 `auth2Login`，不是 `auth2Login`**（拼写无 typo，就这名）
- 公钥**只有 1024 位**，密文是 128 字节而不是 256

## 依赖

见 `requirements.txt`。Python 3.14 + venv。

---

## QQ 消息转发工具

基于 OneBot v11 协议 + NapCatQQ，事件驱动转发，无需模拟鼠标键盘。

### 前置：安装 NapCatQQ

1. 下载 [NapCatQQ](https://github.com/NapNeko/NapCatQQ/releases)（Shell 版）
2. 解压后运行启动脚本，扫码登录 QQ
3. 登录成功后打开 WebUI：`http://127.0.0.1:6099/webui?token=<控制台显示的token>`

### NapCat 网络配置

在 WebUI → **网络配置** → **OneBot11** 中添加 **WebSocket 服务端**：

| 配置项 | 推荐值 | 说明 |
|--------|--------|------|
| 名称 | 随意 | 仅标识用 |
| Host | `127.0.0.1` | 本地连接 |
| Port | `3001` | 默认端口 |
| Token | 自定义 | 填入工具 GUI 的 Token 字段 |
| 消息格式 | **Array** | 必须选 Array，否则图片等富媒体无法转发 |
| 上报自身消息 | **关闭** | 防止转发循环 |
| 强制推送事件 | **开启** | 确保事件及时推送 |
| 心跳间隔 | `30000` | 默认即可 |

配置完成后 NapCat 日志应显示：
```
[OneBot11] [network] WebSocket服务: 127.0.0.1:3001 : 已启动
```

### 使用

1. 启动 NapCat 并完成 QQ 登录
2. 运行 `qq_forward.py`
3. 填写配置：
   - **OneBot WS**: `ws://127.0.0.1:3001`
   - **Token**: NapCat 配置的 Token
   - **源群号**: 要监听的 QQ 群号（纯数字）
   - **目标群号**: 转发目标的 QQ 群号（纯数字）
4. 点击 **启动监控**
5. 源群新消息将自动转发到目标群，格式为 `【发送者】消息内容`

### 转发能力

| 类型 | 支持 | 说明 |
|------|------|------|
| 纯文本 | ✅ | 原样转发 |
| 图片 | ✅ | 原样转发 |
| 表情 | ✅ | 原样转发 |
| 回复 | ✅ | 转为普通消息 |
| 文件/语音/视频 | ❌ | OneBot v11 不支持 |
| @某人 | ✅ | 保留 @ 信息 |

### 注意事项

- 源群和目标群必须是 Bot（登录的 QQ 号）已加入的群
- Bot 在群内需要发言权限
- 不建议源群和目标群相同（会循环转发）

### #指令系统

集成在 `qq_forward.py` 中，由管理员 QQ 在私聊/群聊触发：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 管理员QQ | `3602653998` | 仅该 QQ 可触发 `#` 指令 |
| 启用 #指令 | 开启 | GUI 复选框可一键关闭 |

| 指令 | 行为 |
|------|------|
| `#更新我的课表` | 用 `accounts.json` 中存的 `portal_ticket` 桥接教务系统 → 拉取课表 → 保存到 `schedules/` → 回复"拉取课表成功" → 合并转发当次的 `.json` 和 `.xlsx` 文件给触发方 |

失败时回复：`拉取失败 请检查cookie是否过期 或密码是否错误。`
此时需在课表工具中重新登录（验证码登录）以刷新 `portal_ticket`。

---

## QQ 群历史消息拉取与合并转发

从指定群拉取最近 N 条历史消息，以合并转发形式发送到目标群。复用 NapCat WebSocket 服务（与 QQ 转发工具共用），无需额外配置。

### 使用

1. 启动 NapCat 并完成 QQ 登录
2. 运行 `qq_history.py`
3. 填写配置：
   - **WS 地址**: `ws://127.0.0.1:3001`（与 QQ 转发工具相同）
   - **Token**: NapCat 配置的 Token
   - **源群号**: 拉取历史消息的群号
   - **目标群号**: 合并转发目标的群号
   - **消息条数**: 1-100，默认 20
4. 点击 **拉取并转发**
