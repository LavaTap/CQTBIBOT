# 二课活动详情数据表说明

## 新增表结构
```sql
CREATE TABLE IF NOT EXISTS second_class_activity_detail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id TEXT NOT NULL UNIQUE,
    student_id TEXT NOT NULL,
    qq INTEGER DEFAULT 0,
    activity_name TEXT DEFAULT '',
    module_name TEXT DEFAULT '',
    module_id TEXT DEFAULT '',
    overview TEXT DEFAULT '',  -- 概述（实施内容与方式）
    location TEXT DEFAULT '',  -- 活动地点
    duration TEXT DEFAULT '',  -- 发放时长
    need_sign_out TEXT DEFAULT '',  -- 活动签退（是/否）
    need_summary TEXT DEFAULT '',  -- 需要提交总结（是/否）
    detail_html TEXT DEFAULT '',  -- 活动详情HTML
    organizer TEXT DEFAULT '',
    start_date TEXT DEFAULT '',
    end_date TEXT DEFAULT '',
    apply_start TEXT DEFAULT '',
    apply_end TEXT DEFAULT '',
    status_code TEXT DEFAULT '',
    status_name TEXT DEFAULT '',
    fetched_at TEXT DEFAULT ''
)
```

## 修复的问题
1. **module_name字段错误填充问题**：修复了解析逻辑，避免将整个页面文本错误填充到module_name字段
2. **标签匹配精度问题**：改进了标签匹配算法，精确匹配标签文本
3. **长文本过滤**：增加了对过长文本的过滤，避免解析出异常内容

## 使用示例
```python
# 获取并保存活动详情
sess = obtain_secondclass_session_from_user(user)
detail = fetch_and_save_activity_detail(sess, student_id, activity_id, qq)

# 查询活动详情
db = SecondClassMasterDB()
detail = db.get_activity_detail(activity_id)
```