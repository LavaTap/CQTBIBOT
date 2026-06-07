# 重庆工商职业学院 — 官方接口文档

> 从项目源码逆向整理，涵盖 SSO 认证、教务系统、第二课堂三大子系统。
> 敏感参数（appid/secret/公钥）见 `md/CQTBI.md`。

---

## 一、SSO 统一认证中心

基础地址：`http://szxy.cqtbi.edu.cn/oauth2`

### 1.1 获取授权码（auth2orize）

| 项目 | 值 |
|------|-----|
| URL | `/v1/auth2orize` |
| 方法 | GET |
| 认证 | 无（首次访问） |

**请求参数：**

| 参数 | 说明 | 示例 |
|------|------|------|
| client_id | 应用 appid | `sso20240408001` |
| state | 回调状态 | `callback` |
| redirect_uri | 回调地址 | `http://szxy.cqtbi.edu.cn/oauth2/api-docs/access_token.html` |

**响应：** 302 重定向到 `Login.html?accKey=<accKey>`，从 Location 解析 `accKey`。

> accKey 一次性使用，登录失败后需重新获取。

### 1.2 获取验证码图片

| 项目 | 值 |
|------|-----|
| URL | `/v1/createVertifyCode` |
| 方法 | GET |

**请求参数：**

| 参数 | 说明 |
|------|------|
| checkId | 即 accKey |

**响应：** 图片二进制（JPEG），Content-Type: `image/jpeg`

### 1.3 登录提交（auth2Login）

| 项目 | 值 |
|------|-----|
| URL | `/v1/auth2Login` |
| 方法 | POST |

**请求头：**

```
X-Requested-With: XMLHttpRequest
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Origin: http://szxy.cqtbi.edu.cn
Referer: http://szxy.cqtbi.edu.cn/oauth2/Login.html?accKey=<accKey>
```

**请求参数（form-urlencoded）：**

| 参数 | 说明 |
|------|------|
| ucode | 学号/工号 |
| upwd | RSA-1024 PKCS#1 v1.5 加密后的 base64 字符串 |
| accKey | 授权会话标识 |
| checkId | 同 accKey |
| rcode | 验证码 |

**成功响应：**

```json
{
  "success": true,
  "ticket": "PORTAL_TICKET值",
  "redirect_url": "http://szxy.cqtbi.edu.cn/oauth2/api-docs/access_token.html?code=xxx&state=callback"
}
```

从 `redirect_url` 解析 `code`，从 `ticket` 获取 `PORTAL_TICKET`。

### 1.4 授权码换令牌（access_token）

| 项目 | 值 |
|------|-----|
| URL | `/v1/access_token` |
| 方法 | GET |

**请求参数：**

| 参数 | 说明 | 示例 |
|------|------|------|
| client_id | appid | `sso20240408001` |
| secret | 应用密钥 | `002658359667427995860705420291461` |
| code | 授权码 | 来自 1.3 的 redirect_url |

**响应：**

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

### 1.5 获取用户信息（access_user）

| 项目 | 值 |
|------|-----|
| URL | `/v1/access_user` |
| 方法 | POST |

**请求参数：**

| 参数 | 说明 |
|------|------|
| access_token | 访问令牌 |

**响应：**

```json
{
  "success": true,
  "userloginid": "00763",
  "userrealname": "XXX",
  "userdeptid": "100213",
  "userdepaname": "网络安全与信息化处",
  "teacher": "1"
}
```

### 1.6 票据获取用户数据（getUserDataByTicket）

| 项目 | 值 |
|------|-----|
| URL | `/v1/getUserDataByTicket` |
| 方法 | POST |

**请求参数（form-urlencoded）：**

| 参数 | 说明 |
|------|------|
| PORTAL_TICKET | 门户票据 |

**响应：**

```json
{
  "data": {
    "userstate": "在职",
    "test_user": "否",
    "userrealname": "秦XX",
    "userloginid": "教师工号或学生学号",
    "depaname": "二级单位或班级",
    "dept_id": "单位编号或班级编号",
    "teacher_or_student": "1：教师，0学生"
  },
  "signature": "DCE1E6275CA7DDA5B22AC02D62EFD484",
  "success": true,
  "timestamp": 1766629020709
}
```

### 1.7 注销登录（auth2LoginOut）

| 项目 | 值 |
|------|-----|
| URL | `/v1/auth2LoginOut` |
| 方法 | GET |

**请求参数：**

| 参数 | 说明 |
|------|------|
| access_token | 访问令牌 |
| redirect_url | 注销后跳转地址（需 UTF-8 URL 编码） |

---

## 二、门户中转

基础地址：`http://szxy.cqtbi.edu.cn`

### 2.1 门户登录（dtLog!log.action）

| 项目 | 值 |
|------|-----|
| URL | `/cqdddt/dtLog!log.action` |
| 方法 | POST |
| 用途 | 用 PORTAL_TICKET 设门户 cookie，为后续桥接做准备 |

**请求头：**

```
X-Requested-With: XMLHttpRequest
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Origin: http://szxy.cqtbi.edu.cn
Referer: http://szxy.cqtbi.edu.cn/cqdddt/services.html
```

**请求参数（form-urlencoded）：**

| 参数 | 说明 |
|------|------|
| id | 空 |
| CreateTime | 当前毫秒时间戳 |
| TrackId | 空 |
| ticket | PORTAL_TICKET |
| PORTAL_TICKET | 同 ticket |

**响应：** Set-Cookie: `PORTAL_TICKET=xxx`

### 2.2 自动授权获取 PORTAL_TICKET

通过 `auth2orize` 接口（1.1），若 SSO 侧有活跃会话，会自动 302 到 `redirect_uri?code=xxx`。跟随重定向后门户会设 `PORTAL_TICKET` cookie。

流程：`GET auth2orize → 302 → 跟随重定向 → 从 cookie 取 PORTAL_TICKET`

---

## 三、教务系统（正方 JWGL）

基础地址：`https://jwgl.cqtbi.edu.cn`

### 3.1 JWGL 桥接登录

| 项目 | 值 |
|------|-----|
| URL | `/Logon.do?method=toCqgszy&PORTAL_TICKET={ticket}` |
| 方法 | GET |
| 用途 | 用门户票据桥接进教务，获取 `bzb_jsxsd` cookie |

**流程：**
1. `GET https://jwgl.cqtbi.edu.cn/` — 获取基础 cookie
2. `GET /Logon.do?method=toCqgszy&PORTAL_TICKET=<ticket>` — 桥接
3. 成功后 Set-Cookie: `bzb_jsxsd=xxx`

> 课表请求必须携带 `bzb_jsxsd` cookie。端口 81（`https://jwgl.cqtbi.edu.cn:81`）用于课表接口。

### 3.2 获取课表

| 项目 | 值 |
|------|-----|
| URL | `https://jwgl.cqtbi.edu.cn:81/jsxsd/xskb/xskb_list.do` |
| 方法 | POST |
| 认证 | Cookie: `bzb_jsxsd=xxx` |

**请求参数（form-urlencoded）：**

| 参数 | 说明 | 示例 |
|------|------|------|
| xnxq01id | 学年学期 | `2025-2026-2` |
| zc | 周次 | `5`（空则当前周） |

**响应：** HTML，含课表 `<table>` 结构，需 BeautifulSoup 解析。

### 3.3 成绩查询（cjcx_list）

| 项目 | 值 |
|------|-----|
| URL | `https://jwgl.cqtbi.edu.cn:81/jsxsd/kscj/cjcx_list` |
| 方法 | POST |
| 认证 | Cookie: `bzb_jsxsd=xxx` |
| 格式 | HTML（表格） |

**请求头：**

```
Host: jwgl.cqtbi.edu.cn:81
Content-Type: application/x-www-form-urlencoded
Origin: https://jwgl.cqtbi.edu.cn:81
Referer: https://jwgl.cqtbi.edu.cn:81/jsxsd/kscj/cjcx_query
Cookie: bzb_jsxsd=xxx; bzb_njw=xxx
```

**请求参数（form-urlencoded）：**

| 参数 | 说明 | 示例 |
|------|------|------|
| `kksj` | 开课时间（学期筛选） | 空 |
| `kcxz` | 课程性质 | 空 |
| `kcsx` | 课程属性 | 空 |
| `kcmc` | 课程名称（模糊搜索） | 空 |
| `xsfs` | 显示方式 | `all`（全部） |
| `sfxsbcxq1` | 是否显示补重修 | 空 |
| `mold` | 模式 | 空 |

**curl 示例：**

```bash
curl -X POST "https://jwgl.cqtbi.edu.cn:81/jsxsd/kscj/cjcx_list" \
  -H "Host: jwgl.cqtbi.edu.cn:81" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Origin: https://jwgl.cqtbi.edu.cn:81" \
  -H "Referer: https://jwgl.cqtbi.edu.cn:81/jsxsd/kscj/cjcx_query" \
  -H "Cookie: bzb_jsxsd=xxx; bzb_njw=xxx" \
  --data "kksj=&kcxz=&kcsx=&kcmc=&xsfs=all&sfxsbcxq1=&mold="
```

**响应：** HTML 表格，包含以下字段：

| 字段 | 说明 |
|------|------|
| 学年学期 | 如 `2025-2026-2` |
| 课程代码 | 课程编号 |
| 课程名称 | 课程名称 |
| 课程性质 | 必修/选修 |
| 学分 | 学分值 |
| 补重学期 | 补考/重修学期 |
| 总评成绩 | 最终成绩 |
| 考试性质 | 正常/补考/重修 |
| 绩点 | 学分绩点（如有） |

**前置步骤：** 需要先访问 `cjcx_query` 页面获取必要 cookie：

| 步骤 | 说明 |
|------|------|
| 1 | `GET https://jwgl.cqtbi.edu.cn:81/jsxsd/kscj/cjcx_query` — 进入查询页面 |
| 2 | `POST cjcx_list` — 提交查询条件获取成绩数据 |

## 四、第二课堂系统

基础地址：`https://2class.cqtbi.edu.cn`

### 4.1 桥接登录（cqtbiSSO）

| 项目 | 值 |
|------|-----|
| URL | `/Admin/Index/cqtbiSSO` |
| 方法 | GET |
| 用途 | 用 PORTAL_TICKET 换取二课 SSID |

**请求参数：**

| 参数 | 说明 |
|------|------|
| PORTAL_TICKET | 门户票据 |

**必要请求头（必须用安卓 UA）：**

```
User-Agent: Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36 EdgA/149.0.0.0
sec-ch-ua-mobile: ?1
sec-ch-ua-platform: Android
Referer: https://2class.cqtbi.edu.cn/Student/Activity/index.html
```

> **关键：** 必须用安卓 UA，桌面 UA 虽然返回 200 但 SSID 不可用。

**响应：** Set-Cookie: `SSID=xxx`，后续所有二课请求需携带此 cookie。

**完整桥接流程：**
1. 门户登录 `dtLog!log.action`（二.1）→ 设 PORTAL_TICKET cookie
2. `GET /Admin/Index/cqtbiSSO?PORTAL_TICKET=<ticket>` → 设 SSID cookie

### 4.2 二课首页（index.html）

| 项目 | 值 |
|------|-----|
| URL | `/Student/My/index.html` |
| 方法 | GET |
| 认证 | Cookie: `SSID=xxx` |
| 格式 | HTML |

**解析目标：**

| 数据 | CSS 选择器 | 说明 |
|------|-----------|------|
| 姓名 | `.my_head .name` | 文本内容 |
| 学号 | `.my_head .desc` | 正则 `学号：(\d+)` |
| 学院 | `.my_head .desc` | 正则 `学院：(.+)` |
| 专业 | `.my_head .desc` | 正则 `专业：(.+)` |
| 班级 | `.my_head .desc` | 正则 `班级：(.+)` |
| 我的活动 | `li.my1 span` | 活动总数 |
| 未签到活动 | `li.my10 span` | 未签到数 |
| 未提交总结 | `li.my11 span` | 未提交总结数 |
| 我的项目认定 | `li.my2 span` | 项目认定数 |
| 我学习的课程 | `li.my12 span` | 课程数 |
| 我的社团 | `li.my4 span` | 社团数 |
| 累计时长 | 全文正则 | `累计时长[^\d]*(\d+\.?\d*)[^\d]*小时` |

**会话过期检测：** 响应含 `top.location.href` 或 302 重定向 = SSO 会话过期。

### 4.3 总积分（myScoreTotalGetData）

| 项目 | 值 |
|------|-----|
| URL | `/Student/My/myScoreTotalGetData.html` |
| 方法 | POST |
| 认证 | Cookie: `SSID=xxx` |

**请求头：**

```
X-Requested-With: XMLHttpRequest
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Referer: https://2class.cqtbi.edu.cn/Student/My/myScoreTotal.html?ret=
```

**请求体（两种模式）：**

| 模式 | Body | 说明 |
|------|------|------|
| 全部学年总积分 | `{}`（空 body） | 返回所有学年的分类积分 |
| 指定学年积分 | `yearID=20252026&termID=` | 按 yearID 过滤学年 |

**响应格式 A（数组）：**

```json
[
  {"name": "思想成长", "score": 18.5, "categoryName": "思想成长", "totalScore": 18.5},
  {"name": "专业技能", "score": 22.3, "categoryName": "专业技能", "totalScore": 22.3},
  {"name": "职业技能", "score": 11.1, "categoryName": "职业技能", "totalScore": 11.1}
]
```

**响应格式 B（对象）：**

```json
{
  "totalScore": 51.9,
  "thoughtScore": 18.5,
  "skillScore": 22.3,
  "careerScore": 11.1,
  "sxcz": 18.5,
  "zyjn": 22.3,
  "zyjn2": 11.1
}
```

| 字段 | 别名 | 说明 |
|------|------|------|
| totalScore / totalScoreSum | — | 总积分 |
| thoughtScore / sxcz | name="思想成长" | 思想成长积分 |
| skillScore / zyjn | name="专业技能" | 专业技能积分 |
| careerScore / zyjn2 | name="职业技能" | 职业技能积分 |

### 4.4 模块分类积分（myScoreTotalGetDataByModuleID）

| 项目 | 值 |
|------|-----|
| URL | `/Student/My/myScoreTotalGetDataByModuleID.html` |
| 方法 | POST |
| 认证 | Cookie: `SSID=xxx` |

**请求头：**

```
X-Requested-With: XMLHttpRequest
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Accept: application/json, text/javascript, */*; q=0.01
Origin: https://2class.cqtbi.edu.cn
Referer: https://2class.cqtbi.edu.cn/Student/My/myScoreTotal.html?ret=
```

**请求参数：**

| 参数 | 说明 | 示例 |
|------|------|------|
| moduleID | 模块ID | `2`=思想政治，`4`=实践美育 |
| yearID | 学年ID | `20252026`（空=全部学年） |
| termID | 学期ID | 空 |

**响应：** JSON 数组

```json
[
  {"name": "思想成长与价值引领", "score": 18.5},
  {"name": "劳动教育", "score": 5.0},
  {"name": "文艺美育", "score": 3.0},
  {"name": "志愿服务", "score": 2.0}
]
```

**分类匹配规则：**

| moduleID | 响应 name 关键词 | 字段名 |
|----------|-----------------|--------|
| 2 | — | `ideology_score` |
| 4 | "劳动" | `labor_score` |
| 4 | "文艺"/"美育" | `art_score` |
| 4 | "志愿" | `volunteer_score` |

### 4.5 可报名活动列表（getActivityCanApply）

| 项目 | 值 |
|------|-----|
| URL | `/Student/Activity/getActivityCanApply.html` |
| 方法 | POST |
| 认证 | Cookie: `SSID=xxx` |

**请求头：**

```
X-Requested-With: XMLHttpRequest
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Referer: https://2class.cqtbi.edu.cn/Student/Activity/index.html
```

**请求参数：**

| 参数 | 说明 | 示例 |
|------|------|------|
| moduleID | 模块ID（筛选） | 空 |
| typeID | 类型ID（筛选） | 空 |
| keywords | 关键词搜索 | |
| sortByTime | 按时间排序 | `desc` |
| sortByScore | 按积分排序 | `desc` |

**响应：** JSON，多种格式兼容

格式1: `[maxID, [activity, ...]]`
格式2: `[activity, ...]`
格式3: `{rows: [...], total: N}`

**活动字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| activityID | int | 活动 ID |
| activityName | string | 活动名称 |
| moduleName | string | 模块名称 |
| moduleID | string | 模块 ID |
| score | float | 可获得积分 |
| organizerName | string | 主办方 |
| startDate | int | 开始时间（Unix 时间戳） |
| endDate | int | 结束时间（Unix 时间戳） |
| applyStartDate | int | 报名开始时间（Unix 时间戳） |
| applyEndDate | int | 报名结束时间（Unix 时间戳） |
| status | string | 状态码（见下表） |
| status2Name | string | 二级状态名称 |
| img | string | 活动图片文件名 |
| checkAfterApply | string | 报名后审核（0=否, 1=是, 2=抽签） |
| limitCollege | string | 限制学院（空=全校） |
| limitGrade | string | 限制年级 |
| isClosed | string | 是否已取消（0/1） |
| summaryRequired | int | 是否需提交总结（0/1） |

**活动状态码：**

| 状态码 | 含义 |
|--------|------|
| 0 | 草稿 |
| 1 | 待审核 |
| 2 | 审核未通过 |
| 3 | 报名中 |
| 4 | 报名结束 |
| 5 | 活动中 |
| 6 | 已结束 |
| 7 | 已取消 |

### 4.6 活动详情页（apply.html）

| 项目 | 值 |
|------|-----|
| URL | `/Student/Activity/apply.html` |
| 方法 | GET |
| 认证 | Cookie: `SSID=xxx` |
| 格式 | HTML |

**请求参数：**

| 参数 | 说明 |
|------|------|
| activityID | 活动 ID |
| retUrl | 返回地址，如 `/Student/Activity/index.html` |

**解析字段（HTML 标签相邻匹配 + 正则回退）：**

| 字段 | 页面标签 | 说明 |
|------|---------|------|
| activity_name | 活动名称 | 活动名称 |
| module_name | 模块名称 / 类别名称 | 类别优先 |
| overview | 实施内容与方式 / 活动概述 | 活动描述 |
| location | 活动地点 | 校区/教室 |
| duration | 发放时长 | 如"2小时" |
| need_sign_out | 活动签退 | 是/否 |
| need_summary | 需要提交总结 | 是/否 |
| apply_time | 报名时间 | 分行格式：日期+至+日期 |
| activity_time | 活动时间 | 同上 |
| organizer | 主办方 | 主办方名称 |

### 4.7 我的活动（myActivity）

| 项目 | 值 |
|------|-----|
| URL | `/Student/My/myActivity.html` |
| 方法 | GET |
| 认证 | Cookie: `SSID=xxx` |
| 格式 | HTML（tab1/2/5 服务端渲染）+ AJAX（tab3/4） |

**标签页：**

| tab ID | 名称 | 数据来源 |
|--------|------|---------|
| 1 | 报名中 | HTML 内嵌 |
| 2 | 活动中 | HTML 内嵌 |
| 3 | 已结束 | AJAX 分页 |
| 4 | 其它 | AJAX 分页 |
| 5 | 未开始 | HTML 内嵌 |

**AJAX 分页接口：**

| 标签页 | URL |
|--------|-----|
| 已结束 | `/Student/My/myActivity_End.html` |
| 其它 | `/Student/My/myActivity_other.html` |

**AJAX 请求参数：**

| 参数 | 说明 |
|------|------|
| p | 页码 |
| type | 类型（仅 tab3=已结束时传空） |
| maxActivityID | 分页游标 |

**AJAX 响应格式：** `[maxID, [activity, ...]]`

活动字段同 4.5，额外字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| signDate | int/null | 签到时间 |
| needSignOut | int | 是否需签退（1/0） |
| signOutDate | int/null | 签退时间 |
| ifSummary | int/null | 是否已提交总结 |
| studentScore | float/null | 获取的积分 |

---

## 五、接口调用时序

### 5.1 SSO 登录完整流程

```
客户端                SSO 服务器
  │                     │
  │── GET /v1/auth2orize ──→│  (带 client_id, redirect_uri)
  │←─ 302 Login.html?accKey=xxx ──│
  │                     │
  │── GET /v1/createVertifyCode ──→│  (checkId=accKey)
  │←─ 验证码图片 ────────│
  │                     │
  │── POST /v1/auth2Login ──→│  (ucode, upwd=RSA(pw), accKey, rcode)
  │←─ {success, ticket, redirect_url?code=xxx} ──│
  │                     │
  │── GET /v1/access_token ──→│  (client_id, secret, code)
  │←─ {access_token, expires_in} ──│
```

### 5.2 课表获取流程

```
SSO 登录 → portal_ticket
    │
    ├── POST /cqdddt/dtLog!log.action  (设 PORTAL_TICKET cookie)
    │
    ├── GET /Logon.do?method=toCqgszy&PORTAL_TICKET=xxx  (桥接 JWGL)
    │    → Set-Cookie: bzb_jsxsd
    │
    └── POST :81/jsxsd/xskb/xskb_list.do  (获取课表 HTML)
         Cookie: bzb_jsxsd
```

### 5.3 二课获取流程

```
SSO 登录 → portal_ticket
    │
    ├── POST /cqdddt/dtLog!log.action  (设 PORTAL_TICKET cookie)
    │
    ├── GET /Admin/Index/cqtbiSSO?PORTAL_TICKET=xxx  (桥接二课)
    │    → Set-Cookie: SSID
    │    ⚠️ 必须用安卓 UA
    │
    ├── GET /Student/My/index.html          → 活动计数+身份信息
    ├── POST /Student/My/myScoreTotalGetData.html  → 总积分
    ├── POST /Student/My/myScoreTotalGetDataByModuleID.html  → 分类积分
    ├── POST /Student/Activity/getActivityCanApply.html  → 可报名活动
    ├── GET /Student/My/myActivity.html      → 我的活动
    └── GET /Student/Activity/apply.html     → 活动详情
```

---

## 六、通用注意事项

| 项目 | 说明 |
|------|------|
| SSL | 教务和二课服务器 TLS 配置老旧，需禁用证书校验 + 降低 SSL 最低版本 |
| 代理 | `session.trust_env = False` 避免本地代理干扰 |
| 二课 UA | 桥接必须安卓 UA，桌面 UA 返回的 SSID 不可用 |
| 密码加密 | RSA-1024 PKCS#1 v1.5，公钥见 `md/CQTBI.md` |
| accKey | 一次性使用，登录失败后必须重新获取 |
| 会话过期 | 二课页面含 `top.location.href` = 会话过期，需重新桥接 |
| SSID 缓存 | 二课 SSID 可复用，定期验证即可，不必每次桥接 |
