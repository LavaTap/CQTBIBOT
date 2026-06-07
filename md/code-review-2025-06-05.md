# 核心代码评审报告

> 评审日期：2025-06-05
> 评审范围：core/account_store.py, core/user_session.py, sso/sso_common.py, sso/sso_login.py, schedule/schedule_tool.py, qq/qq_forward.py

---

## 1. `core/account_store.py` — 账号存储

**评分：8.5 / 10**

### 优点
- 函数职责单一，每个函数只做一件事
- 类型注解完整，IDE 友好
- `ensure_account` 智能合并：已有字段保留旧值，新值覆盖
- `save_account` 支持按 QQ 或学号匹配更新

### 问题与建议

| 严重度 | 位置 | 问题 | 建议 |
|--------|------|------|------|
| 🟡 中 | `update_account_field` L59-68 | 每次更新单个字段都全量读写 `accounts.json`，低效 | 延迟保存或批量更新 |
| 🟡 中 | `ensure_account` L94-101 | 匹配逻辑 `qq == 或 student_id ==`，若 qq 不同但 student_id 相同会错误覆盖 | 优先精确匹配 QQ，不命中再按 student_id |
| 🔵 低 | 全局 | `load_data()` 依赖 `sso.sso_common`，形成间接耦合 | 可接受，当前无循环引用 |

---

## 2. `core/user_session.py` — 会话管理器

**评分：9 / 10**

### 优点
- `threading.Lock` 保护全局 `_sessions` 字典，线程安全
- `__slots__` 优化 `UserSession` 内存占用
- daemon 清理线程不会阻止进程退出
- `is_expired` / `is_active` property 设计清晰

### 问题与建议

| 严重度 | 位置 | 问题 | 建议 |
|--------|------|------|------|
| 🟡 中 | `_cleanup_loop` L113 | `while True: time.sleep(60)` 无退出机制，只能靠 daemon 强制终止 | 加 `threading.Event` 支持优雅关闭 |
| 🔵 低 | `create_or_get_session` L75-84 | 始终更新 `msg_type` 和 `group_id`，可能与已有状态冲突 | 仅在创建时设置，或明确注释行为 |

---

## 3. `sso/sso_common.py` — SSO 共享基础设施

**评分：9 / 10**

### 优点
- `redact()` 脱敏机制完善：同时支持 form-urlencoded 和 JSON 两种格式
- `install_request_logging()` 通过 requests hooks 无侵入记录 HTTP 日志 —— **值得全项目推广**的最佳实践
- `setup_logging()` 幂等设计，输出到 stderr，符合 12-Factor App 规范
- 常量命名规范、URL 模板化

### 问题与建议

| 严重度 | 位置 | 问题 | 建议 |
|--------|------|------|------|
| 🔥 高 | L29 `SECRET` | 密钥硬编码在源码中，存在泄露风险 | 改为 `os.getenv("SSO_SECRET", "")` + `.env` 文件 |
| 🟡 中 | `exchange_code_for_token` L185 | 每次调用新建 `requests.Session()`，无法复用连接池 | 复用模块级 session 或传入 session 参数 |
| 🔵 低 | L199 `data.get("success")` | 未显式检查 `is True` | 类型安全建议 `data.get("success") is True` |

---

## 4. `sso/sso_login.py` — SSO 自动登录 GUI

**评分：8 / 10**

### 优点
- RSA 加密密码（PKCS#1 v1.5），不传输明文
- `trust_env = False` 避免系统代理干扰
- 登录失败后自动 `begin()` 重拉 accKey，容错性佳
- 区分 `LoginError` 和 `Exception` 的异常处理

### 问题与建议

| 严重度 | 位置 | 问题 | 建议 |
|--------|------|------|------|
| 🟡 中 | `_do_login_bg` L365 | `except Exception` 裸捕获 | 缩小到已知异常类型 |
| 🟡 中 | L173 `LoginError(result.get("msg"))` | 错误消息可能包含敏感信息 | 对 msg 做 `redact()` 处理 |
| 🔵 低 | `_build_ui` | 200 行 UI 代码全在一个方法里 | 拆分为 `_build_account_ui()`、`_build_captcha_ui()` 等子方法 |

---

## 5. `schedule/schedule_tool.py` — 课表工具

**评分：7.5 / 10**

### 优点
- `Course` / `Schedule` dataclass + `to_dict()` 序列化清晰
- `ScheduleParser` 详尽注释了正方教务 HTML 结构，便于维护
- `JWGLClient` 分步骤登录流程完整（begin_sso → sso_login → portal_login → bridge → get_schedule）
- Excel 导出支持按周分 sheet，样式丰富实用

### 问题与建议

| 严重度 | 位置 | 问题 | 建议 |
|--------|------|------|------|
| 🟡 中 | `_parse_div` L392 | `re.split(r"-{5,}", str(div))` 若课程名或教室名含 5 个以上短横线，会错误分割 | 使用更严格的分隔上下文匹配 |
| 🟡 中 | `_bg_login_and_fetch` L1201 | 后台线程无 `finally` 保护 UI 忙状态，若异常未被 catch 会永久卡住 | 确保所有路径都调用 `_set_busy(False)` |
| 🟡 中 | `_max_week` L136 | 硬编码默认 20 周 | 应从课程数据中推算实际最大周 |
| 🔵 低 | 文件 1538 行 | 文件偏大，不利于维护 | 可按 `parser` / `client` / `gui` / `export` 拆分 |

---

## 6. `qq/qq_forward.py` — QQ 消息转发

**评分：6 / 10**

### 优点
- `OneBotClient` 封装 WebSocket 通信，API 结构清晰
- `_seen_ids` 去重机制防止循环转发
- 自动重连 watchdog，保障长期运行
- 指令系统设计清晰（`#` 前缀 + 多步会话）

### 问题与建议

| 严重度 | 位置 | 问题 | 建议 |
|--------|------|------|------|
| 🔥🔥 **关键** | `try_handle` L472-1887 | **1400+ 行的巨型方法**，所有指令处理逻辑塞在一起 | 每个指令拆为独立方法，或独立到 `qq/commands.py` |
| 🔥🔥 **关键** | 全文件 | 与 `monitor_forward.py` **大量重复**：CommandHandler 的指令列表（#帮助、#登录、#扫码登录等 20+ 指令）、SSO 登录流程、课表拉取逻辑几乎完全一样 | 抽取共享层到 `qq/common.py` |
| 🔥 高 | `UserDB.upsert` L90-116 | 10 个位置参数 + SQL `CASE WHEN` 条件更新，过于复杂 | 改用 dataclass 或 `TypedDict` 组织参数 |
| 🔥 高 | `_forward_message` L2056 | 先发发送者标识再发内容，若第一步成功第二步失败，目标群收到不完整消息 | 用合并转发或事务语义 |
| 🟡 中 | L2018 | `_seen_ids > 10000` 硬编码阈值 | 改为配置项 |
| 🟡 中 | L488-673 | 每个指令用 `threading.Thread(target=..., daemon=True)` 启动 | 高频场景使用 `ThreadPoolExecutor` |
| 🔵 低 | `_reconnect_watchdog` L2069 | 固定 5 秒重试，日志可能过于频繁 | 指数退避（1s→2s→4s→...→60s） |

---

## 跨文件共性问题

| 问题 | 影响范围 | 严重度 | 建议 |
|------|---------|--------|------|
| **密钥硬编码** | `sso_common.py`, `schedule/export_classroom_schedule.py` | 🔥 高 | 统一 `.env` + `os.getenv()` |
| **代码重复** | `qq_forward.py` ↔ `monitor_forward.py` | 🔥🔥 关键 | 抽取 `qq/common.py` 共享层 |
| **文件过大** | `schedule_tool.py`(1538行), `qq_forward.py`(2320行), `monitor_forward.py`(2390行) | 🔥 高 | 按职责拆分为多个文件 |
| **异常处理过宽** | 多处 `except Exception`（部分有 `# noqa: BLE001`） | 🟡 中 | 缩小到已知异常类型 |
| **dict 类型缺泛型** | 几乎全部文件 | 🔵 低 | `dict` → `dict[str, Any]` |

---

## 优先改进建议

| 优先级 | 行动 | 预计收益 |
|--------|------|---------|
| **P0** | `qq_forward.py` + `monitor_forward.py` 去重，消除数百行重复逻辑 | 减少 30%+ 的重复代码 |
| **P0** | `try_handle` 1400 行拆分到独立文件 | 可维护性大幅提升 |
| **P1** | 密钥统一管理（`.env` + `os.getenv`） | 消除安全风险 |
| **P1** | 大文件拆分（按职责分模块） | 提升可读性和可测试性 |
| **P2** | `except Exception` 缩小范围 | 减少隐藏 bug |
| **P2** | 统一使用 `core/account_store.py` 的 `ensure_account` | 消除重复写入逻辑 |

---

*报告生成由 CodeBuddy 自动完成*
