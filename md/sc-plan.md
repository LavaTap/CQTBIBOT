# 二课自动积分工具实现计划

## Context

当前二课积分极低（总分0.7），各模块均未达标（思想成长需7分/0分，专业技能需0.6分/0分，职业精神需18分/0.7分）。现有代码只有手动报名（QQ机器人`#报名`命令）和定时数据拉取，缺少自动报名、自动签到/签退、自动提交总结功能。需要构建全自动化工具来刷积分。

## 新发现的API

`POST /Student/My/getScoreDataJson.html` — 返回详细积分数据（当前积分 vs 需求积分，按模块分解），现有代码未使用。

---

## 实现分4个阶段

### 阶段1：积分仪表盘

**修改文件**: `secondclass/secondclass_tool.py`（新增API函数）+ 新建 `secondclass_auto_score.py`（CLI入口）

1. 在 `secondclass_tool.py` 新增 `fetch_score_data_json(sess, year_term)` 函数
   - 调用 `POST /Student/My/getScoreDataJson.html`，body: `yearTerm=20252026-2`
   - 返回: `{student_info, scoreTotal, scoreTotalLimit, modules, MaxScore, hoursTotal}`

2. 新建 `secondclass_auto_score.py`，实现 `status` 命令
   - 终端表格展示：当前积分 / 需求积分 / 差距，按模块分解
   - 调用现有 `fetch_index_counts_with_session` 显示未签到/未总结数量

### 阶段2：批量自动报名

**修改文件**: `secondclass/secondclass_tool.py`（新增函数）+ `requirements.txt`

1. 新增 `auto_recognize_captcha(sess)` — 用 ddddocr 自动识别验证码
2. 新增 `auto_signup_activity(sess, activity_id)` — 封装报名流程（获取页面→识别验证码→提交）
3. 新增 `filter_eligible_activities(activities, student_college, student_grade, needed_modules)` — 按学院/年级/模块缺口过滤
4. 实现 `signup` 命令：
   - 获取可报名活动列表 → 过滤 → 按模块缺口排序 → 逐个报名
   - 支持 `--dry-run`（仅显示不报名）和 `--max N`（限制数量）
   - 每次报名间隔 2-4 秒随机延迟

新增依赖: `ddddocr>=1.5`（验证码OCR识别库）

### 阶段3：签到/签退/总结API逆向

**关键前提**: 签到/签退/总结的API端点尚未发现，需要逆向。

逆向策略（按优先级）：
1. **浏览器抓包**: 用DevTools拦截"我的活动"页面的签到/签退按钮请求
2. **URL探测**: 系统性测试候选端点（`/Student/Activity/signIn.html`、`/Student/Activity/signOut.html`、`/Student/Activity/submitSummary.html`等）
3. **JS源码分析**: 从`myActivity.html`页面提取JS中的AJAX调用

在 `secondclass_tool.py` 新增：
- `probe_endpoints(sess, activity_id)` — 系统性探测候选端点
- `submit_activity_sign_in(sess, activity_id, **kwargs)` — 签到
- `submit_activity_sign_out(sess, activity_id, **kwargs)` — 签退
- `submit_activity_summary(sess, activity_id, summary_text)` — 提交总结
- `fetch_my_activities_needing_action(sess)` — 返回需签到/签退/总结的活动列表

定位签到处理：预配置校园坐标，支持自定义经纬度。

### 阶段4：监控循环 + 全自动模式

1. 实现活动状态机: 报名中 → 需签到 → 已签到 → 需签退 → 已签退 → 需总结 → 完成
2. 实现定时触发:
   - 活动开始前30秒尝试签到
   - 活动结束后60秒尝试签退
   - 签退后自动提交总结（模板生成100-300字）
3. 状态持久化: `_temp/auto_score_state.json` 记录每个活动的处理状态
4. 实现 `monitor` 命令（仅监控循环）和 `run` 命令（报名+监控全自动）
5. 自动总结生成: 基于活动名称/模块/概述的模板系统

---

## 风险控制

| 风险 | 对策 |
|------|------|
| 验证码识别失败 | ddddocr最多重试3次，仍失败则跳过该活动 |
| 请求频率过高 | 每次请求间隔2-4秒随机延迟，失败指数退避 |
| SSID过期 | 捕获SecondClassAuthError，自动重认证 |
| 定位签到 | 默认跳过定位签到活动，可配置校园坐标 |
| 签到/签退端点未知 | 先实现探测函数，运行时动态发现 |
| 报名限制 | 解析limit_college/limit_grade，仅报名符合条件的活动 |

## 配置文件

`_temp/auto_score_config.json`:
```json
{
  "year_term": "20252026-2",
  "campus_latitude": "29.97",
  "campus_longitude": "106.27",
  "poll_interval_seconds": 60,
  "signup_delay_seconds": 3.0,
  "sign_in_early_seconds": 30,
  "sign_out_late_seconds": 60,
  "max_captcha_retries": 3,
  "skip_location_sign_in": true
}
```

## 验证方式

1. `python secondclass_auto_score.py status` — 确认积分仪表盘正确显示
2. `python secondclass_auto_score.py signup --dry-run` — 确认过滤逻辑正确
3. `python secondclass_auto_score.py signup --max 1` — 先报名1个活动验证流程
4. `python secondclass_auto_score.py probe` — 探测签到/签退/总结端点
5. `python secondclass_auto_score.py monitor` — 监控已报名活动并自动签到
6. `python secondclass_auto_score.py run` — 全自动模式

### 阶段5：图形界面（GUI）

**新增文件**: `secondclass_auto_score_gui.py`

基于 Tkinter 的独立刷分工具图形窗口，提供与 `secondclass_auto_score.py` CLI 等价的功能，以按钮/表单形式操作。

**界面布局**：

```text
┌─ 二课自动刷分工具 ─────────────────────────────────┐
│                                                      │
│ ┌─ 凭证信息 ──────────────────────────────────────┐ │
│ │  SSID: [___________________________] [获取SSID]   │ │
│ │  学号: 2403740  姓名: xxx  学院: 电子信息工程    │ │
│ └──────────────────────────────────────────────────┘ │
│                                                      │
│ ┌─ 积分仪表盘 ──────────────────────────────────────┐ │
│ │  总积分: 6.1 / 需达标: 25.0  [!! 未达标]         │ │
│ │  ┌──────────────────────┬──────┬──────┬──────┐   │ │
│ │  │ 模块                 │ 当前 │ 需   │ 差距  │   │ │
│ │  ├──────────────────────┼──────┼──────┼──────┤   │ │
│ │  │ 思想成长             │ 0.0  │ 7.0  │ -7.0 │   │ │
│ │  │ 专业技能             │ 0.0  │ 0.6  │ -0.6 │   │ │
│ │  │ 职业精神             │ 0.7  │ 18.0 │ -17.3│   │ │
│ │  └──────────────────────┴──────┴──────┴──────┘   │ │
│ │  活动: 78  未签到: 18  未总结: 16  社团: 2        │ │
│ └──────────────────────────────────────────────────┘ │
│                                                      │
│ ┌─ 操作面板 ────────────────────────────────────────┐ │
│ │  [刷新仪表盘]  [预览可报名]  [批量报名]  [探测]    │ │
│ │  [启动监控]  [停止监控]  [全自动运行]              │ │
│ │  报名数量: [10▾]  间隔秒: [3▾]                    │ │
│ └──────────────────────────────────────────────────┘ │
│                                                      │
│ ┌─ 运行日志 ────────────────────────────────────────┐ │
│ │  23:59:59 [INFO] 正在获取可报名活动列表...         │ │
│ │  23:59:59 [INFO] 找到 15 个可报名活动              │ │
│ │  23:59:59 [INFO] 报名成功: xxx (0.1分)             │ │
│ │  [自动滚动]                                    [清空]│
│ └──────────────────────────────────────────────────┘ │
│                                                      │
│ ┌─ 实时状态面板 ────────────────────────────────────┐ │
│ │  状态: 运行中 ✓    已报名: 5/10   已签到: 0/3    │ │
│ │  已签退: 0/1   已总结: 0/2   上次扫描: 23:59:00  │ │
│ └──────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────┘
```

**实现功能**：

| 功能 | 对应 CLI 命令 | 说明 |
|------|-------------|------|
| 获取 SSID | `ticket --json accounts.json` | 从本地凭证获取 SSID，或手动输入 |
| 刷新仪表盘 | `status` | 显示积分全景 + 未签到/未总结统计 |
| 预览可报名 | `signup --dry-run` | 按模块缺口排序，勾选想报名的活动 |
| 批量报名 | `signup --max N` | 后台线程逐个报名，实时日志 |
| 探测 | `probe` | 探测签到/签退/总结端点 |
| 启动/停止监控 | `monitor` | 定时轮询，自动签到/签退/总结 |
| 全自动运行 | `run` | 报名 → 监控，一步到位 |

**关键设计点**：
1. 复用在 `secondclass_auto_score.py` 中已有的函数（`cmd_status`、`auto_signup_activity`、`ActivityMonitor` 等）
2. 后台线程执行所有网络操作，Tkinter 主线程仅处理 UI 更新
3. `_LogHandler` 模式（同 `secondclass_tool_gui.py`）将日志输出到文本框
4. 支持 `--ssid` 参数启动时直连，或通过"获取 SSID"按钮从本地凭证自动获取
5. 状态持久化复用 `_temp/auto_score_state.json`

## 关键文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `secondclass/secondclass_auto_score.py` | 已创建 | CLI主模块（含6个子命令） |
| `secondclass/secondclass_auto_score_gui.py` | 新建 | 图形界面窗口 |
| `secondclass/secondclass_tool.py` | 已修改 | 新增API函数 + `convert_to_ssid()` |
| `requirements.txt` | 修改 | 新增ddddocr依赖 |
