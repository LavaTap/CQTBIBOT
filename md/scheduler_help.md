# 二课调度器使用说明

## 一、票据Token状态判定逻辑

二课系统的票据生命周期分为三层：`access_token` → `portal_ticket` → `SSID`，调度器通过以下步骤判定状态有效性：

1. **缓存SSID快速验证**
   在`secondclass_tool._get_cached_session`中：
   - 尝试使用缓存的`SSID`创建会话
   - 通过请求轻量接口`/Student/My/index.html`快速验证：
     - 响应状态码是否为200
     - 响应中是否包含`top.location.href`（重定向到登录页，说明会话已过期）
   - 如果验证失败，自动清除过期缓存

2. **完整凭证有效性校验**
   在`obtain_secondclass_session_from_user`中：
   - 首先检查`access_token`的有效期`expires_at`，如果已过期直接抛出认证异常
   - 如果`portal_ticket`缺失，自动使用`access_token`调用`_obtain_portal_ticket`获取新的门户票据
   - 尝试桥接SSID：通过门户登录和二课SSO接口获取有效`SSID`
   - 如果桥接失败，抛出`SecondClassAuthError`

3. **调度器中的状态分类**
   在`run_once`遍历用户时：
   - 捕获`SecondClassAuthError` → 标记为**过期用户**（SSID/凭证失效）
   - 捕获其他异常 → 标记为**拉取失败用户**（接口调用、解析错误等）
   - 成功完成拉取 → 标记为**活跃用户**

## 二、单次运行拉取所有账户

当前`secondclass_scheduler.py`的`run_once`方法**已经默认拉取所有账户**，实现逻辑如下：
1.  通过`secondclass_tool.get_all_user_credentials()`获取所有用户的凭证（从`users`表和`accounts.json`中读取，去重后返回所有用户）
2.  遍历每个用户，依次执行会话获取、数据拉取、存储逻辑
3.  `--once`参数启动时，会直接调用`run_once()`，完成所有用户的拉取

### 使用命令
```bash
# 单次拉取所有用户数据
python secondclass_scheduler.py --once

# 后台守护模式运行
python secondclass_scheduler.py --daemon

# 正常调度模式（按策略定时拉取）
python secondclass_scheduler.py
```