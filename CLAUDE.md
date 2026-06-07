# SSO Tools — 教务工具集 | CLAUDE.md

> **轻量开发指南** | 完整开发者文档请参阅 `DEVELOPER.md`

---

## 一、项目简介

重庆工商职业学院教务工具集，基于 SSO 统一认证体系，实现：
- **SSO 自动登录** — OAuth2 授权码流程，管理 token/票据
- **课表管理** — 学生/教室课表获取、对比、导出
- **QQ 转发** — OneBot v11 群消息自动转发 + #指令系统
- **二课系统** — 积分查询、活动列表、自动调度
- **抓包代理** — mitmproxy 抓取 SSO 登录参数

---

## 二、架构概览

```
入口层         核心层             存储层           外部服务
sso_login.py ──→ sso/sso_common.py ──→ accounts.json ──→ SSO 认证中心
sso_tool.py  ──→ sso/sso_common.py ──→               ──→ (抓包代理)
qq_forward.py ─→ sso/sso_common.py ──→ users.db      ──→ NapCat (OneBot)
                 core/user_session.py                 ──→ 教务系统
                 core/account_store.py                ──→ 二课系统
```

---

## 三、知识源索引

| 文档 | 位置 | 最佳用途 |
|------|------|---------|
| `DEVELOPER.md` | 项目根目录 | **完整开发者手册**：功能实现、DB 表结构、API 接口、配置说明 |
| `CLAUDE.md` | 项目根目录（本文档） | **轻量概览**：架构概述、技术栈、快速参考 |
| `md/class.md` | md/ 目录 | **二课 API 文档**：接口参数、响应格式 |
| `md/CQTBI.md` | md/ 目录 | **学校特有参数**：appid/secret/公钥 |

---

## 四、智能体命令

| 命令 | 说明 |
|------|------|
| `/guide` | 输出 DEVELOPER.md 概览 |
| `/guide ask <问题>` | 智能路由问答（架构→CLAUDE.md，功能→DEVELOPER.md） |
| `/guide update` | 增量更新 DEVELOPER.md |
| `/guide update --full` | 全量重写 DEVELOPER.md |
| `/guide claude` | 轻量模式（仅使用本文件） |

---

## 五、常用命令

```bash
pip install -r requirements.txt      # 安装依赖
python sso_login.py                   # 启动 SSO 登录工具
python qq_forward.py                  # 启动 QQ 转发
python schedule_tool.py               # 启动课表工具
python secondclass_tool_gui.py        # 启动二课调度 GUI
python secondclass_scheduler.py       # 启动二课后台调度器
ruff format . && ruff check . --fix   # 格式化 + Lint
```

---

## 六、核心约束

| 规则 | 说明 |
|------|------|
| 禁止提交 | 不提交 accounts.json, users.db, venv/, __pycache__/, .env |
| 禁止后台线程操作 UI | Tkinter 操作必须在主线程 |
| 禁止静默异常 | 所有 except 块必须记录日志 |
| 敏感信息脱敏 | 日志中密码/token 用 `***` 替代 |
