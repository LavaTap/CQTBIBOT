---
title: secondclass auto score token login design
date: 2026-06-05
---

# 二课自动积分 token 登录设计

## 目标

修复 `secondclass_auto_score.bat` 在 Windows 终端中因编码不匹配导致的菜单命令乱码和执行失败，并将 `secondclass_auto_score_gui.py` 顶部连接方式改为只输入 `access_token`，点击连接后自动换取二课 `SSID` 并建立会话。

## 推荐方案

采用共享认证路径：GUI 不自行拼接 SSO 请求，而是调用 `secondclass_tool.convert_to_ssid(access_token=token)`。该函数负责 access_token → portal_ticket → SSID 的桥接流程，GUI 只负责输入、状态显示、连接验证。

## 组件变更

- `secondclass_auto_score.bat`
  - 菜单文本改为 ASCII-only，避免中文源码和 `chcp` 不一致造成命令被拆坏。
  - 保留原菜单编号和对应 CLI 子命令。
  - 保持参数直通：带参数时仍执行 `python secondclass_auto_score.py %*`。

- `secondclass_auto_score_gui.py`
  - 顶部标签从 `SSID` 改为 `access_token`。
  - 输入变量从 SSID 语义改为 token 语义。
  - 点击“连接”时先调用 `convert_to_ssid(access_token=token)`。
  - 获取 SSID 成功后使用 `_session(ssid)` 创建会话，并访问二课首页验证有效性。
  - 不在日志或界面中输出完整 token/SSID。

- `secondclass/secondclass_tool.py`
  - 仅在测试暴露出 token-only 路径有问题时做最小修正。
  - 保持现有 portal_ticket 和缓存逻辑不变。

## 数据流

1. 用户在 GUI 顶部输入 `access_token`。
2. GUI 调用 `convert_to_ssid(access_token=token)`。
3. 底层通过 SSO 自动授权尝试获取 `portal_ticket`。
4. 底层通过门户桥接和 `cqtbiSSO` 获取二课 `SSID` cookie。
5. GUI 用 SSID 创建二课 session。
6. GUI 请求 `/Student/My/index.html` 验证 session 可用。
7. 验证成功后刷新积分仪表盘。

## 错误处理

- token 为空：提示输入 `access_token`。
- token 无法换取 portal_ticket 或 SSID：状态显示转换失败，并记录脱敏日志。
- SSID 验证失败：状态显示登录已过期或连接失败。
- 后台线程不直接操作 Tkinter UI；所有 UI 更新继续通过主线程路径执行。

## 测试与验证

- 添加或调整单元测试，覆盖 `convert_to_ssid(access_token=...)` token-only 路径会调用 portal_ticket 获取和桥接流程。
- 添加 GUI 方法级测试，验证 `_connect` 会把 access_token 传给 `convert_to_ssid`，并用返回的 SSID 创建 session。
- 运行相关 pytest。
- 通过 `cmd /c secondclass_auto_score.bat status` 验证 bat 不再出现乱码拆命令错误；如本环境无法执行 Windows cmd，则说明未能手动验证。