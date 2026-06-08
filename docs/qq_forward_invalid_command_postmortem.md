# `#指令` 在 QQ 转发工具被标记「无效」的根因总结

## 现象时间线

```
[18:29:19] 收到事件: message/group group=493527490
[18:29:19] 忽略非源群消息 group=493527490（源群列表=[264092896]）
           指令无效
```

随后修复 `#报名` 流程，又出现：

```
✨ #报名 121499
。 正在准备报名活动 121499…
。 二课登录已过期，请重新 #扫码登录
```

`#二课信息` 刚刚成功过，但 `#报名` 立刻报「登录过期」，说明并非真正的鉴权失效。

## 根因 1：改错了文件，运行的是另一个 CommandHandler

项目里有两个并存的 QQ 入口：

| 文件 | 用途 | 内部指令处理器 |
|------|------|--------------|
| `qq/monitor_forward.py` | 早期 / 测试用监控转发 | `CommandHandler`（基于 `core/user_session.SessionStep`） |
| `qq/qq_forward.py` | **当前实际运行的转发工具** | 自带独立 `CommandHandler`（基于 `_LoginState` / `_LoginSession` dataclass） |

第一次修复时把 `#报名` 加在了 `qq/monitor_forward.py` 上，但用户运行的是 `qq/qq_forward.py`，所以新指令根本没被注册，落入 `try_handle → return False`，再走到「忽略非源群消息」分支后，最终被 OneBot 端回了「指令无效」。

**教训**：改指令一定先 grep `try_handle` / 指令常量定义，确认对应的就是当前进程里的那一个 handler。两个 `CommandHandler` 不互相 import，状态机也不通用，必须分别维护。

## 根因 2：`fetch_apply_page` 的 cookie 名大小写写反

`secondclass_tool.py:3213`：

```python
# 错误：cookie 实际名是 "SSID"
if "login" in r.url.lower() or "ssid" not in [c.name for c in sess.cookies]:
    raise SecondClassAuthError("二课登录已过期，请重新 #扫码登录")
```

`requests.Session.cookies` 里的 cookie 名是大小写敏感的，二课服务端下发的就是大写 `SSID`。
`"ssid" not in ["SSID", ...]` 永远为 True，所以**任何访问 `apply.html` 的调用都会一上来就抛 `SecondClassAuthError`**，与真实登录状态无关。

`#二课信息` 走 `fetch_all_with_session`、不经过这里，因此正常；`#报名` 第一步就触发，整个流程被假阳性拦截。

**修复**：

```python
cookie_names = {c.name for c in sess.cookies}
if "login" in r.url.lower() or "SSID" not in cookie_names:
    log.warning("fetch_apply_page 疑似登录失效: url=%s cookies=%s status=%s",
                r.url, sorted(cookie_names), r.status_code)
    raise SecondClassAuthError("二课登录已过期，请重新 #扫码登录")
```

附带补充诊断日志（url / cookies / status），下次类似问题可以一眼看到具体原因，不再需要怀疑「过期了？还是 cookie 名写错了？」。

## 经验教训

1. **同名抽象不要做两份**：`qq_forward.py` 和 `monitor_forward.py` 各有一个 `CommandHandler` 和会话状态枚举，长期来看应合并到一个共享层（建议复用 `core/user_session.SessionStep` 这套），否则每加一条 `#指令` 都要双写、双测。
2. **Cookie 名按字面比对**：HTTP cookie 名是大小写敏感的；任何 `c.name` 比对都不要做 `.lower()` 或写不同 case 的字面量。
3. **「登录已过期」类异常一定要带上下文日志**：原代码只抛了同一句话，没有 url / cookies / status，调试时只能靠 grep 字符串反推位置，浪费时间。修复时统一加日志。
4. **看到「指令无效」先确认指令到底注册没注册**：转发工具的指令列表零散分布在 `try_handle` 一长串 `if text == ...` 里，新增时最好在 `_help_text()`（或 `help_image.py` 的 `ALL_COMMANDS`）同步登记，方便用户和自己核对。
