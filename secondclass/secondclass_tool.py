"""第二课堂（2class.cqtbi.edu.cn）工具模块。

从二课系统抓取活动、积分信息，存入 users.db 的 second_class 表。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import ssl
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup, NavigableString
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("secondclass")

# ── 常量 ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent

USER_DB_FILE = PROJECT_ROOT / "users.db"
BASE_URL = "https://2class.cqtbi.edu.cn"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 安卓 UA + 完整头（cqtbiSSO 桥接必须用这些头才能拿到有效 SSID）
ANDROID_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 EdgA/149.0.0.0"
    ),
    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="149", "Edge";v="149"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": "Android",
    "Upgrade-Insecure-Requests": "1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "Accept-Encoding": "gzip, deflate, br, zstd",
}


# ════════════════════ DB ════════════════════


class SecondClassDB:
    """users.db 中 second_class 表的操作。"""

    def __init__(self, db_path: Path = USER_DB_FILE) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            # second_class_v2 以 student_id 为主键，qq 降为普通列
            conn.execute("""
                CREATE TABLE IF NOT EXISTS second_class_v2 (
                    student_id TEXT PRIMARY KEY,
                    qq INTEGER DEFAULT 0,
                    realname TEXT DEFAULT '',
                    deptname TEXT DEFAULT '',
                    college TEXT DEFAULT '',
                    major TEXT DEFAULT '',
                    total_score REAL DEFAULT 0,
                    activity_count INTEGER DEFAULT 0,
                    unsigned_count INTEGER DEFAULT 0,
                    unfinished_count INTEGER DEFAULT 0,
                    club_count INTEGER DEFAULT 0,
                    thought_score REAL DEFAULT 0,
                    skill_score REAL DEFAULT 0,
                    career_score REAL DEFAULT 0,
                    year_id TEXT DEFAULT '',
                    total_score_all REAL DEFAULT 0,
                    thought_score_all REAL DEFAULT 0,
                    skill_score_all REAL DEFAULT 0,
                    career_score_all REAL DEFAULT 0,
                    ideology_score REAL DEFAULT 0,
                    labor_score REAL DEFAULT 0,
                    art_score REAL DEFAULT 0,
                    volunteer_score REAL DEFAULT 0,
                    ideology_score_all REAL DEFAULT 0,
                    labor_score_all REAL DEFAULT 0,
                    art_score_all REAL DEFAULT 0,
                    volunteer_score_all REAL DEFAULT 0,
                    total_duration REAL DEFAULT 0,
                    total_duration_all REAL DEFAULT 0,
                    ideology_duration REAL DEFAULT 0,
                    labor_duration REAL DEFAULT 0,
                    art_duration REAL DEFAULT 0,
                    volunteer_duration REAL DEFAULT 0,
                    ideology_duration_all REAL DEFAULT 0,
                    labor_duration_all REAL DEFAULT 0,
                    art_duration_all REAL DEFAULT 0,
                    volunteer_duration_all REAL DEFAULT 0,
                    semester_info TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)
            # 清理僵尸旧表（second_class 数据已迁移到 second_class_v2）
            conn.execute("DROP TABLE IF EXISTS second_class")
            conn.commit()
            conn.close()

    def upsert(self, student_id: str, qq: int = 0, **kw) -> None:
        """插入或更新二课数据（写入 second_class_v2，以 student_id 为主键）。"""
        if not student_id:
            log.warning("upsert 跳过: student_id 为空")
            return
        fields = [
            "realname",
            "deptname",
            "college",
            "major",
            "total_score",
            "activity_count",
            "unsigned_count",
            "unfinished_count",
            "club_count",
            "thought_score",
            "skill_score",
            "career_score",
            "year_id",
            "total_score_all",
            "thought_score_all",
            "skill_score_all",
            "career_score_all",
            # 四类积分（本学期）
            "ideology_score",
            "labor_score",
            "art_score",
            "volunteer_score",
            # 四类积分（全学年）
            "ideology_score_all",
            "labor_score_all",
            "art_score_all",
            "volunteer_score_all",
            # 时长数据
            "total_duration",
            "total_duration_all",
            "ideology_duration",
            "labor_duration",
            "art_duration",
            "volunteer_duration",
            "ideology_duration_all",
            "labor_duration_all",
            "art_duration_all",
            "volunteer_duration_all",
            "semester_info",
        ]
        text_fields = {
            "realname",
            "deptname",
            "college",
            "major",
            "year_id",
            "semester_info",
        }
        now_iso = datetime.now().isoformat(timespec="seconds")
        updates = ", ".join([f"{f}=excluded.{f}" for f in fields])
        values = [kw.get(f, "" if f in text_fields else 0) for f in fields]

        with self._lock:
            conn = self._connect()
            conn.execute(
                f"""
                INSERT INTO second_class_v2 (student_id, qq, {", ".join(fields)}, updated_at)
                VALUES (?, ?, {", ".join(["?"] * len(fields))}, ?)
                ON CONFLICT(student_id) DO UPDATE SET
                    qq=CASE WHEN excluded.qq!=0 THEN excluded.qq ELSE second_class_v2.qq END,
                    {updates},
                    updated_at=excluded.updated_at
            """,
                [student_id, qq] + values + [now_iso],
            )
            conn.commit()
            conn.close()
        log.info("二课数据已保存: student_id=%s qq=%s", student_id, qq)

    def get_by_qq(self, qq: int) -> dict | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM second_class_v2 WHERE qq=?", (qq,)
            ).fetchone()
            conn.close()
            return dict(row) if row else None

    def get_by_student_id(self, student_id: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM second_class_v2 WHERE student_id=?", (student_id,)
            ).fetchone()
            conn.close()
            return dict(row) if row else None


# ════════════════════ 二课总表 DB ════════════════════


class SecondClassMasterDB:
    """users.db 中 second_class_master 表的操作。
    存储所有用户的完整二课快照数据，用于自动调度拉取。
    """

    def __init__(self, db_path: Path = USER_DB_FILE) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            # second_class_master_v2：以 (student_id, activity_id) 为唯一键
            conn.execute("""
                CREATE TABLE IF NOT EXISTS second_class_master_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT NOT NULL,
                    qq INTEGER DEFAULT 0,
                    activity_id TEXT NOT NULL,
                    activity_name TEXT DEFAULT '',
                    module_name TEXT DEFAULT '',
                    module_id TEXT DEFAULT '',
                    score REAL DEFAULT 0,
                    organizer TEXT DEFAULT '',
                    start_date TEXT DEFAULT '',
                    end_date TEXT DEFAULT '',
                    apply_start TEXT DEFAULT '',
                    apply_end TEXT DEFAULT '',
                    status_code TEXT DEFAULT '',
                    status_name TEXT DEFAULT '',
                    check_after_apply TEXT DEFAULT '',
                    limit_college TEXT DEFAULT '',
                    limit_grade TEXT DEFAULT '',
                    img TEXT DEFAULT '',
                    is_closed TEXT DEFAULT '0',
                    fetched_at TEXT DEFAULT ''
                )
            """)
            # 唯一约束：每个学生的每个活动只保留一条记录
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_master_v2_sid_activity
                ON second_class_master_v2(student_id, activity_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_master_v2_sid
                ON second_class_master_v2(student_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_master_v2_fetched
                ON second_class_master_v2(fetched_at)
            """)
            # 清理僵尸旧表
            conn.execute("DROP TABLE IF EXISTS second_class_master")
            # 清理旧的单列 activity_id 唯一索引（已改为组合索引）
            try:
                conn.execute("DROP INDEX IF EXISTS idx_master_v2_activity_id")
            except Exception:
                pass
            conn.commit()
            conn.close()

    def upsert_activities(
        self, student_id: str, activities: list[dict], qq: int = 0
    ) -> int:
        """批量插入或更新活动（写入 v2 表，按 student_id+activity_id 去重）。"""
        if not activities:
            return 0
        if not student_id:
            log.warning("upsert_activities 跳过: student_id 为空")
            return 0
        now_iso = datetime.now().isoformat(timespec="seconds")
        count = 0
        with self._lock:
            conn = self._connect()
            for act in activities:
                conn.execute(
                    """
                    INSERT INTO second_class_master_v2
                        (student_id, qq, activity_id, activity_name, module_name, module_id,
                         score, organizer, start_date, end_date,
                         apply_start, apply_end, status_code, status_name,
                         check_after_apply, limit_college, limit_grade,
                         img, is_closed, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(student_id, activity_id) DO UPDATE SET
                        activity_name=excluded.activity_name,
                        module_name=excluded.module_name,
                        score=excluded.score,
                        organizer=excluded.organizer,
                        start_date=excluded.start_date,
                        end_date=excluded.end_date,
                        apply_start=excluded.apply_start,
                        apply_end=excluded.apply_end,
                        status_code=excluded.status_code,
                        status_name=excluded.status_name,
                        is_closed=excluded.is_closed,
                        fetched_at=excluded.fetched_at
                """,
                    (
                        student_id,
                        qq,
                        act["activity_id"],
                        act.get("activity_name", ""),
                        act.get("module_name", ""),
                        act.get("module_id", ""),
                        act.get("score", 0),
                        act.get("organizer", ""),
                        act.get("start_date", ""),
                        act.get("end_date", ""),
                        act.get("apply_start", ""),
                        act.get("apply_end", ""),
                        act.get("status_code", ""),
                        act.get("status_name", ""),
                        act.get("check_after_apply", ""),
                        act.get("limit_college", ""),
                        act.get("limit_grade", ""),
                        act.get("img", ""),
                        act.get("is_closed", "0"),
                        now_iso,
                    ),
                )
                count += 1
            conn.commit()
            conn.close()
        return count

    def get_expired_users(self) -> list[str]:
        """返回所有有活动记录的 student_id 列表。"""
        with self._lock:
            conn = self._connect()
            rows = conn.execute("""
                SELECT DISTINCT student_id FROM second_class_master_v2
                WHERE student_id != ''
            """).fetchall()
            conn.close()
            return [r["student_id"] for r in rows]

    def get_by_student_id(self, student_id: str) -> list[dict]:
        """按 student_id 查询所有活动记录。"""
        if not student_id:
            return []
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM second_class_master_v2 WHERE student_id=? "
                "ORDER BY fetched_at DESC",
                (student_id,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]

    def delete_by_activity_id(self, activity_id: str) -> bool:
        """按 activity_id 删除 master 记录。"""
        if not activity_id:
            return False
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM second_class_master_v2 WHERE activity_id=?", (activity_id,)
            )
            deleted = cur.rowcount > 0
            conn.commit()
            conn.close()
            return deleted

    def clean_old_data(self, older_than_hours: int = 24) -> int:
        """清理超过指定小时未更新的数据。"""
        with self._lock:
            conn = self._connect()
            count = conn.execute(
                "SELECT COUNT(*) FROM second_class_master_v2"
            ).fetchone()[0]
            conn.close()
            return count

    def get_activities_by_student_id(self, student_id: str) -> list[dict]:
        """根据学生ID获取所有二课活动，按更新时间倒序排列。"""
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM second_class_master_v2 WHERE student_id=? ORDER BY fetched_at DESC",
                (student_id,),
            ).fetchall()
            conn.close()
            return [dict(row) for row in rows] if rows else []

    def get_activity_detail_by_id(self, activity_id: str) -> dict | None:
        """根据活动ID获取活动详情（从 v3 表）。"""
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM second_class_activity_detail_v3 WHERE activity_id=?",
                (activity_id,),
            ).fetchone()
            conn.close()
            return dict(row) if row else None


# ════════════════════ 活动详情 DB（v3）════════════════════


class SecondClassActivityDetailDB:
    """users.db 中 second_class_activity_detail_v3 表的操作。

    存储每个活动的详细页面信息（从 apply.html 解析），按 activity_id 去重。
    新增字段：apply_time（报名时间）、activity_time（活动时间）。
    """

    def __init__(self, db_path: Path = USER_DB_FILE) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS second_class_activity_detail_v3 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activity_id TEXT NOT NULL UNIQUE,
                    activity_name TEXT DEFAULT '',
                    module_name TEXT DEFAULT '',
                    category_name TEXT DEFAULT '',
                    implementation_method TEXT DEFAULT '',
                    overview TEXT DEFAULT '',
                    score_detail TEXT DEFAULT '',
                    organizer TEXT DEFAULT '',
                    contact_phone TEXT DEFAULT '',
                    location TEXT DEFAULT '',
                    duration TEXT DEFAULT '',
                    signup_method TEXT DEFAULT '',
                    max_participants TEXT DEFAULT '',
                    current_participants TEXT DEFAULT '',
                    limit_college TEXT DEFAULT '',
                    limit_grade TEXT DEFAULT '',
                    need_sign_out TEXT DEFAULT '',
                    need_summary TEXT DEFAULT '',
                    organizer_need_summary TEXT DEFAULT '',
                    cancel_time_limit TEXT DEFAULT '',
                    location_sign TEXT DEFAULT '',
                    attachment TEXT DEFAULT '',
                    main_image TEXT DEFAULT '',
                    apply_time TEXT DEFAULT '',
                    activity_time TEXT DEFAULT '',
                    fetched_at TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_detail_v3_activity_id
                ON second_class_activity_detail_v3(activity_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_detail_v3_fetched
                ON second_class_activity_detail_v3(fetched_at)
            """)
            # 迁移旧表：补充新列（幂等）
            _add_cols = [
                "category_name",
                "implementation_method",
                "score_detail",
                "contact_phone",
                "signup_method",
                "max_participants",
                "current_participants",
                "limit_college",
                "limit_grade",
                "organizer_need_summary",
                "cancel_time_limit",
                "location_sign",
                "attachment",
                "main_image",
            ]
            for col in _add_cols:
                try:
                    conn.execute(
                        f"ALTER TABLE second_class_activity_detail_v3 ADD COLUMN {col} TEXT DEFAULT ''"
                    )
                except (sqlite3.OperationalError, Exception):
                    pass  # 列已存在
            # 清理僵尸旧表
            conn.execute("DROP TABLE IF EXISTS second_class_activity_detail")
            conn.execute("DROP TABLE IF EXISTS second_class_activity_detail_v2")
            conn.commit()
            conn.close()

    def upsert(self, activity_id: str, **kw) -> None:
        """插入或更新一条活动详情记录（按 activity_id 去重）。"""
        if not activity_id:
            log.warning("活动详情 upsert 跳过: activity_id 为空")
            return
        fields = [
            "activity_name",
            "module_name",
            "category_name",
            "implementation_method",
            "overview",
            "score_detail",
            "organizer",
            "contact_phone",
            "location",
            "duration",
            "signup_method",
            "max_participants",
            "current_participants",
            "limit_college",
            "limit_grade",
            "need_sign_out",
            "need_summary",
            "organizer_need_summary",
            "cancel_time_limit",
            "location_sign",
            "attachment",
            "main_image",
            "apply_time",
            "activity_time",
        ]
        text_fields = set(fields)
        now_iso = datetime.now().isoformat(timespec="seconds")
        updates = ", ".join([f"{f}=excluded.{f}" for f in fields if f != "activity_id"])
        values = [kw.get(f, "" if f in text_fields else "") for f in fields]

        with self._lock:
            conn = self._connect()
            conn.execute(
                f"""
                INSERT INTO second_class_activity_detail_v3
                    (activity_id, {", ".join(fields)}, fetched_at)
                VALUES (?, {", ".join(["?"] * len(fields))}, ?)
                ON CONFLICT(activity_id) DO UPDATE SET
                    {updates},
                    fetched_at=excluded.fetched_at
            """,
                [activity_id] + values + [now_iso],
            )
            conn.commit()
            conn.close()
        log.info("活动详情已保存: activity_id=%s", activity_id)

    def get_by_activity_id(self, activity_id: str) -> dict | None:
        """按 activity_id 查询活动详情。"""
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM second_class_activity_detail_v3 WHERE activity_id=?",
                (activity_id,),
            ).fetchone()
            conn.close()
            return dict(row) if row else None

    def delete_by_activity_id(self, activity_id: str) -> bool:
        """按 activity_id 删除活动详情记录。"""
        if not activity_id:
            return False
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM second_class_activity_detail_v3 WHERE activity_id=?",
                (activity_id,),
            )
            deleted = cur.rowcount > 0
            conn.commit()
            conn.close()
            if deleted:
                log.info("活动详情已删除: activity_id=%s", activity_id)
            return deleted

    def get_all(self) -> list[dict]:
        """获取所有活动详情记录。"""
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM second_class_activity_detail_v3 ORDER BY fetched_at DESC"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]

    def count(self) -> int:
        """返回活动详情记录总数。"""
        with self._lock:
            conn = self._connect()
            cnt = conn.execute(
                "SELECT COUNT(*) FROM second_class_activity_detail_v3"
            ).fetchone()[0]
            conn.close()
            return cnt

    def get_unfetched_activity_ids(self) -> list[str]:
        """返回 master_v2 表中存在但 detail_v3 表中尚未抓取的活动 ID。"""
        with self._lock:
            conn = self._connect()
            rows = conn.execute("""
                SELECT DISTINCT m.activity_id
                FROM second_class_master_v2 m
                LEFT JOIN second_class_activity_detail_v3 d
                    ON m.activity_id = d.activity_id
                WHERE d.activity_id IS NULL
            """).fetchall()
            conn.close()
            return [r[0] for r in rows]

    def get_need_reparse_activity_ids(self) -> list[str]:
        """返回旧版解析的详情记录（category_name 或 implementation_method 为空）。"""
        with self._lock:
            conn = self._connect()
            rows = conn.execute("""
                SELECT activity_id FROM second_class_activity_detail_v3
                WHERE (category_name IS NULL OR category_name = '')
                   OR (implementation_method IS NULL OR implementation_method = '')
                ORDER BY fetched_at DESC
            """).fetchall()
            conn.close()
            return [r[0] for r in rows]


# ════════════════════ 用户未结束活动 DB ════════════════════


class SecondClassUserActivityDB:
    """users.db 中 second_class_user_activities / second_class_users 表的操作。

    - second_class_user_activities: 1 行/活动/学生（旧版按活动存储）
    - second_class_users:          1 行/学生，unfinished_activity_ids 逗号分隔（按用户存储）

    用于 #我的二课 指令快速查询活动列表。
    """

    def __init__(self, db_path: Path = USER_DB_FILE) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _join(ids: list[str]) -> str:
        seen, out = set(), []
        for x in ids:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return ",".join(out)

    @staticmethod
    def _split(csv: str | None) -> list[str]:
        if not csv:
            return []
        return [x for x in csv.split(",") if x]

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            # 旧表：一活动一行
            conn.execute("""
                CREATE TABLE IF NOT EXISTS second_class_user_activities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq INTEGER DEFAULT 0,
                    student_id TEXT NOT NULL,
                    activity_id TEXT NOT NULL,
                    fetched_at TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_user_act_sid_aid
                ON second_class_user_activities(student_id, activity_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_act_sid
                ON second_class_user_activities(student_id)
            """)
            # 新表：一学生一行，unfinished_activity_ids 逗号分隔
            conn.execute("""
                CREATE TABLE IF NOT EXISTS second_class_users (
                    qq INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL UNIQUE,
                    unfinished_activity_ids TEXT DEFAULT '',
                    reserved_activity_ids TEXT DEFAULT '',
                    reservations_meta TEXT DEFAULT '{}',
                    updated_at TEXT DEFAULT ''
                )
            """)
            conn.commit()
            conn.close()

    def upsert_activities(self, student_id: str, activities: list[dict], qq: int = 0) -> int:
        """批量插入活动ID，按 (student_id, activity_id) 去重。"""
        if not activities or not student_id:
            return 0
        now_iso = datetime.now().isoformat(timespec="seconds")
        count = 0
        with self._lock:
            conn = self._connect()
            for act in activities:
                aid = act.get("activity_id", "")
                if not aid:
                    continue
                conn.execute("""
                    INSERT OR IGNORE INTO second_class_user_activities
                        (qq, student_id, activity_id, fetched_at)
                    VALUES (?, ?, ?, ?)
                """, (qq, student_id, aid, now_iso))
                count += 1
            conn.commit()
            conn.close()
        return count

    def clear_by_student(self, student_id: str) -> int:
        """清空指定学生的所有记录。"""
        if not student_id:
            return 0
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM second_class_user_activities WHERE student_id=?",
                (student_id,),
            )
            deleted = cur.rowcount
            conn.commit()
            conn.close()
        return deleted

    def get_activity_ids_by_student_id(self, student_id: str) -> list[str]:
        """按学号查询所有未结束活动ID列表。"""
        if not student_id:
            return []
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT activity_id FROM second_class_user_activities WHERE student_id=?",
                (student_id,),
            ).fetchall()
            conn.close()
            return [r["activity_id"] for r in rows]

    def get_by_student_id(self, student_id: str) -> list[dict]:
        """按学号查询所有未结束活动记录。"""
        if not student_id:
            return []
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM second_class_user_activities WHERE student_id=?",
                (student_id,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]

    # ════════════════════ second_class_users 操作 ════════════════════

    def update_unfinished_ids(self, *, student_id: str, activity_ids: list[str], qq: int = 0) -> None:
        """更新 second_class_users 表的 unfinished_activity_ids 字段。"""
        if not student_id:
            return
        now_iso = datetime.now().isoformat(timespec="seconds")
        csv = self._join(activity_ids)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT qq FROM second_class_users WHERE student_id=?", (student_id,)
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE second_class_users
                       SET unfinished_activity_ids=?, updated_at=?
                       WHERE student_id=?""",
                    (csv, now_iso, student_id),
                )
            else:
                conn.execute(
                    """INSERT INTO second_class_users
                       (qq, student_id, unfinished_activity_ids, updated_at)
                       VALUES (?, ?, ?, ?)""",
                    (qq or 0, student_id, csv, now_iso),
                )
            conn.commit()
            conn.close()

    def get_unfinished_ids_by_student_id(self, student_id: str) -> list[str]:
        """从 second_class_users.unfinished_activity_ids 读取逗号分隔的活动ID。"""
        if not student_id:
            return []
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT unfinished_activity_ids FROM second_class_users WHERE student_id=?",
                (student_id,),
            ).fetchone()
            conn.close()
        return self._split(row["unfinished_activity_ids"] if row else "")


# ════════════════════ API 调用 ════════════════════


class SecondClassAuthError(RuntimeError):
    pass


def get_current_year_id() -> str:
    """自动计算当前学年 ID（如 2025-2026 学年第二学期 → '20252026'）。"""
    now = datetime.now()
    year = now.year
    month = now.month
    if month >= 9:
        return f"{year}{year + 1}"
    else:
        return f"{year - 1}{year}"


# ── SSL 适配器：解决老旧服务器握手失败（SSLV3_ALERT_HANDSHAKE_FAILURE） ──


class _SSLAdapter(HTTPAdapter):
    """使用宽松 SSL 上下文的请求适配器，兼容老旧服务器 TLS 配置。"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # 允许更广泛的 TLS 版本和密码套件
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(*args, **kwargs)


def _patch_session(sess: requests.Session) -> None:
    """为 session 挂载宽松 SSL 适配器，绕过握手失败问题。"""
    adapter = _SSLAdapter()
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    sess.verify = False
    sess.trust_env = False


def _session(ssid: str = "") -> requests.Session:
    sess = requests.Session()
    sess.trust_env = False
    sess.headers.update(HEADERS)
    _patch_session(sess)
    if ssid:
        sess.cookies.set("SSID", ssid)
    return sess


def obtain_portal_ticket(access_token: str) -> str:
    """用 access_token 通过 SSO 自动授权流程获取 PORTAL_TICKET。

    核心思路：调用 auth2orize，若 SSO 侧有活跃会话会自动 302 到 redirect_uri?code=xxx，
    跟随重定向链后门户会设 PORTAL_TICKET cookie。
    若无活跃会话则无法自动授权，返回空串。
    """
    if not access_token:
        return ""
    from sso.sso_common import APPID, AUTH_URL_TEMPLATE, DEFAULT_REDIRECT

    sess = requests.Session()
    sess.trust_env = False
    sess.headers.update(HEADERS)
    _patch_session(sess)

    try:
        auth_url = AUTH_URL_TEMPLATE.format(appid=APPID, redirect_uri=DEFAULT_REDIRECT)
        r = sess.get(auth_url, allow_redirects=False, timeout=15)
        loc = r.headers.get("Location", "")

        # 自动授权成功：302 → redirect_uri?code=xxx
        if r.status_code in (301, 302, 303, 307) and loc:
            sess.get(loc, timeout=15, allow_redirects=True)

        pt = sess.cookies.get("PORTAL_TICKET")
        if pt:
            log.info("通过 auth2orize 自动授权获取 PORTAL_TICKET: %s…", pt[:8])
            return pt

        log.warning("auth2orize 未自动授权（SSO 无活跃会话），无法获取 PORTAL_TICKET")
        return ""
    except Exception as e:
        log.warning("获取 PORTAL_TICKET 失败: %s", e)
        return ""


# 旧名称兼容别名：外部模块（如 qq_forward.py）可能引用 _obtain_portal_ticket
_obtain_portal_ticket = obtain_portal_ticket


def convert_to_ssid(
    access_token: str = "",
    portal_ticket: str = "",
    *,
    student_id: str = "",
) -> str:
    """将 access_token + portal_ticket 转化为二课 SSID cookie 值。

    适用于已有 access_token/portal_ticket（如从 accounts.json 提取），
    想直接获得 SSID 进行二课 API 调用的场景。

    Args:
        access_token: OAuth2 访问令牌（可选，当 portal_ticket 过期时用于刷新）。
        portal_ticket: 门户票据（可选，二选一或都提供）。
        student_id: 学号（用于 SSID 缓存）。

    Returns:
        SSID cookie 值，失败返回空字符串。

    Example:
        >>> ssid = convert_to_ssid(
        ...     access_token="671108d1609a4624b97ac5d6371db910",
        ...     portal_ticket="00276dbbf60a14145f783b16e77f521d291",
        ... )
        >>> print(ssid)
        'e8f3a1b2c4d5...'
    """
    if not portal_ticket and not access_token:
        log.error("convert_to_ssid: 至少需要 access_token 或 portal_ticket")
        return ""

    # 构建 user dict 复用现有逻辑
    user = {
        "student_id": student_id,
        "portal_ticket": portal_ticket,
        "access_token": access_token,
        "expires_at": 0,
    }

    try:
        sess = obtain_secondclass_session_from_user(user)
        ssid = sess.cookies.get("SSID", "")
        if ssid:
            log.info("convert_to_ssid: 成功获取 SSID=%s…", ssid[:8])
        return ssid
    except SecondClassAuthError as e:
        log.error("convert_to_ssid 失败: %s", e)
        return ""
    except Exception as e:
        log.error("convert_to_ssid 异常: %s", e)
        return ""


# ── SSID 缓存（避免每次调度都重新桥接） ──
_SSID_CACHE: dict[str, str] = {}  # student_id → SSID


def _get_cached_session(student_id: str) -> requests.Session | None:
    """尝试用缓存的 SSID 直接创建会话。"""
    ssid = _SSID_CACHE.get(student_id)
    if ssid:
        sess = _session(ssid)
        # 快速验证：请求一个轻量接口看是否可用
        try:
            r = sess.get(f"{BASE_URL}/Student/My/index.html", timeout=10)
            if r.status_code == 200 and "top.location.href" not in r.text:
                return sess
        except Exception:
            pass
        # SSID 过期，清除缓存
        _SSID_CACHE.pop(student_id, None)
    return None


def _cache_ssid(student_id: str, sess: requests.Session) -> None:
    """缓存成功会话的 SSID。"""
    ssid = sess.cookies.get("SSID")
    if ssid:
        _SSID_CACHE[student_id] = ssid


def obtain_secondclass_session_from_user(user: dict) -> requests.Session:
    portal_ticket = user.get("portal_ticket", "")
    access_token = user.get("access_token", "")
    expires_at = int(user.get("expires_at") or 0)
    student_id = user.get("student_id", "")

    # 1. 优先使用缓存的 SSID（避免每次重新桥接）
    cached = _get_cached_session(student_id)
    if cached:
        log.info("二课：使用缓存 SSID 会话: student_id=%s", student_id)
        return cached

    if expires_at and expires_at < int(time.time()) and not access_token:
        raise SecondClassAuthError("登录已过期，请重新 #扫码登录")
    if not portal_ticket and not access_token:
        raise SecondClassAuthError("缺少登录凭证，请重新 #扫码登录")

    # 如果 portal_ticket 缺失，用 access_token 获取新的 portal_ticket
    if not portal_ticket:
        portal_ticket = _obtain_portal_ticket(access_token)
        if not portal_ticket:
            raise SecondClassAuthError("缺少门户票据，请重新 #扫码登录")

    # 尝试认证：第一步 → 门户 dtLog!log.action，第二步 → 二课 cqtbiSSO
    def try_auth(pt: str) -> requests.Session | None:
        sess = requests.Session()
        sess.trust_env = False
        sess.headers.update(ANDROID_HEADERS)
        _patch_session(sess)
        body = {
            "id": "",
            "CreateTime": str(int(time.time() * 1000)),
            "TrackId": "",
            "ticket": pt,
            "PORTAL_TICKET": pt,
        }
        sess.post(
            "http://szxy.cqtbi.edu.cn/cqdddt/dtLog!log.action",
            data=body,
            timeout=15,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "http://szxy.cqtbi.edu.cn",
                "Referer": "http://szxy.cqtbi.edu.cn/cqdddt/services.html",
            },
        )
        pt_cookie = sess.cookies.get("PORTAL_TICKET")
        if pt_cookie:
            pt = pt_cookie
        sess.get(
            f"{BASE_URL}/Admin/Index/cqtbiSSO",
            params={"PORTAL_TICKET": pt},
            timeout=15,
            allow_redirects=True,
            headers={
                "Referer": "https://2class.cqtbi.edu.cn/Student/Activity/index.html"
            },
        )
        if sess.cookies.get("SSID"):
            return sess
        return None

    # 第一次尝试：使用存储的 portal_ticket
    sess = try_auth(portal_ticket)
    if sess:
        _cache_ssid(student_id, sess)
        return sess

    # 第一次失败 → 用 access_token 获取新 portal_ticket 重试
    log.info("二课：原有 portal_ticket 过期，尝试用 access_token 刷新")
    if not access_token:
        raise SecondClassAuthError(
            "二课登录已过期，请重新 #扫码登录（portal_ticket 失效且无 access_token）"
        )

    new_pt = _obtain_portal_ticket(access_token)
    if not new_pt:
        raise SecondClassAuthError("二课登录已过期，请重新 #扫码登录")

    sess = try_auth(new_pt)
    if sess:
        _cache_ssid(student_id, sess)
        return sess

    raise SecondClassAuthError("二课登录已过期，请重新 #扫码登录（桥接失败）")


# ── 身份解析辅助 ──

_PAGE_TEXT_LOG_LIMIT = 3000
_IDENTITY_TAGS = {"td", "th", "div", "span", "li", "p", "dd", "dt"}
_COLLEGE_PATS = [
    r"学院[：:、\s]+([^\d\s\n,，、;；]+)",
    r"院系[：:、\s]+([^\d\s\n,，、;；]+)",
    r"所属学院[：:、\s]*([^\d\s\n,，、;；]+)",
    r"二级学院[：:、\s]*([^\d\s\n,，、;；]+)",
]
_MAJOR_PATS = [
    r"专业[：:、\s]+([^\d\s\n,，、;；]+(?:\([^)]*\))?)",
    r"专业方向[：:、\s]*([^\d\s\n,，、;；]+)",
]


def _parse_identity_from_page(soup: BeautifulSoup, text: str) -> dict:
    """从 index.html 页面文本中提取学院、专业等身份信息。"""
    result: dict[str, str] = {"college": "", "major": ""}

    # 文本全文扫描（快速路径）
    for pat in _COLLEGE_PATS:
        m = re.search(pat, text)
        if m:
            val = m.group(1).strip().rstrip("\\n").rstrip()
            if val and len(val) >= 2:
                result["college"] = val
                break
    for pat in _MAJOR_PATS:
        m = re.search(pat, text)
        if m:
            val = m.group(1).strip().rstrip("\\n").rstrip()
            if val and len(val) >= 2:
                result["major"] = val
                break

    # DOM 回退搜索：仅在 text 匹配不足时，限制标签范围提升性能
    if not result["college"] or not result["major"]:
        for tag in soup.find_all(_IDENTITY_TAGS):
            t = tag.get_text(strip=True)
            if not result["college"]:
                cm = re.search(r"学院[：:、\s]+([^\d\s\n,，、;；]+)", t)
                if cm:
                    result["college"] = cm.group(1).strip()
            if not result["major"]:
                mm = re.search(r"专业[：:、\s]+([^\d\s\n,，、;；]+)", t)
                if mm:
                    result["major"] = mm.group(1).strip()
            if result["college"] and result["major"]:
                break

    return result


# ── 页面抓取 ──


def fetch_index_counts_with_session(sess: requests.Session) -> dict:
    """从 index.html 解析活动计数及学院/专业身份信息。

    页面 HTML 结构示例：
        <li class="my1"><a href="..."><span>219</span>我的活动</a></li>
        <li class="my10"><a href="..."><span>77</span>未签到活动</a></li>
        <li class="my11"><a href="..."><span>56</span>未提交总结的活动</a></li>
        <li class="my4"><a href="..."><span>24</span>我的社团</a></li>
    """
    r = sess.get(f"{BASE_URL}/Student/My/index.html", timeout=15)

    # ── 检测 SSO 会话过期（被重定向回登录页）──
    if "top.location.href" in r.text or r.status_code in (301, 302, 303, 307):
        log.warning("二课首页被重定向（SSO 会话过期），前200字符: %s", r.text[:200])
        raise SecondClassAuthError("二课登录已过期，请重新 #扫码登录")

    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    result = {
        "activity_count": 0,
        "unsigned_count": 0,
        "unfinished_count": 0,
        "club_count": 0,
        "college": "",
        "major": "",
    }

    # 调试：记录前 3000 字符的 HTML 以便排查
    raw_html_preview = r.text[:3000]
    log.debug("二课首页原始 HTML(前3000字符):\n%s", raw_html_preview)

    # ── 数字解析：遍历 my_list 中的 <li><a><span>数字</span>标签</a></li> ──
    li_elements = soup.find_all("li", class_=re.compile(r"my\d+"))
    if not li_elements:
        log.info(
            "二课首页未找到任何 li(class=myN) 元素，HTML 前500字符: %s", r.text[:500]
        )

    for li in li_elements:
        a = li.find("a")
        span = li.find("span")
        if not (a and span):
            log.debug("跳过 li(class=%s): 缺 a 或 span", li.get("class"))
            continue
        span_text = span.get_text(strip=True)
        num = int(span_text) if span_text.isdigit() else 0

        # ---- 稳健的标签提取 ----
        # 从 <a> 的直接文本子节点提取纯标签，避免嵌套 <span> 的干扰
        label_parts: list[str] = []
        for child in a.children:
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if text:
                    label_parts.append(text)
        label = "".join(label_parts)

        # 如果 NavigableString 方式未提取到标签，回退到原始 replace 方式
        if not label and span_text:
            label = a.get_text(strip=True).replace(span_text, "", 1).strip()

        log.info("二课 li(%s): num=%d label=%s", li.get("class"), num, label)

        if "我的活动" in label:
            result["activity_count"] = num
        elif "未签到" in label:
            result["unsigned_count"] = num
        elif "未提交总结" in label:
            result["unfinished_count"] = num
        elif "我的社团" in label:
            result["club_count"] = num

    # ── 身份解析 ──
    text = soup.get_text("\n", strip=True)
    log.debug(
        "二课首页原始文本(前%d字符): %s",
        _PAGE_TEXT_LOG_LIMIT,
        text[:_PAGE_TEXT_LOG_LIMIT],
    )
    identity = _parse_identity_from_page(soup, text)
    result["college"] = identity.get("college", result["college"])
    result["major"] = identity.get("major", result["major"])

    # 诊断：当所有计数为零时说明认证失败（通常是重定向回门户）
    all_zero = not any(
        result.get(k)
        for k in ("activity_count", "unsigned_count", "unfinished_count", "club_count")
    )
    if all_zero:
        # 全零 + 内容短（< 500 字符）或含 top.location.href → 认证失败
        if len(r.text.strip()) < 500 or "top.location.href" in r.text:
            log.warning("二课首页认证失败（SSO 会话过期），HTML: %s", r.text[:300])
            raise SecondClassAuthError("二课登录已过期，请重新 #扫码登录")
        log.info(
            "二课首页解析结果(全零)，可能无活动数据，HTML 前500字符: %s", r.text[:500]
        )
    else:
        log.info(
            "二课首页解析结果: %s",
            {k: v for k, v in result.items() if k not in ("college", "major")},
        )
    if result["college"]:
        log.info("二课学院: %s", result["college"])
    if result["major"]:
        log.info("二课专业: %s", result["major"])
    return result


def fetch_index_counts(ssid: str) -> dict:
    return fetch_index_counts_with_session(_session(ssid))


def extract_duration_from_index(html: str) -> float:
    """从二课首页HTML中提取累计时长"""
    import re

    duration_match = re.search(r"累计时长[^\d]*(\d+\.?\d*)[^\d]*小时", html)
    if duration_match:
        return float(duration_match.group(1))
    return 0.0


def _safe_json_response(r: requests.Response, context: str = "") -> object:
    """安全解析 JSON 响应，非 JSON（认证失效返回 HTML）时统一抛 SecondClassAuthError。"""
    try:
        return r.json()
    except ValueError as e:
        if hasattr(r, "text"):
            text = r.text.strip()
            if not text:
                log.error("二课(%s)返回空响应，可能认证失败", context)
            else:
                log.error(
                    "二课(%s) JSON 解析失败(可能认证失效): %s\n前500字符: %s",
                    context,
                    e,
                    text[:500],
                )
        else:
            log.error("二课(%s) JSON 解析失败(可能认证失效): %s", context, e)
        raise SecondClassAuthError("二课系统认证失败，请重新 #扫码登录") from e


def _extract_category_from_raw(raw: object) -> dict:
    """从总积分原始 JSON 中提取分类积分（思想成长/专业技能/职业技能）。"""
    thought = skill = career = 0.0
    if isinstance(raw, list):
        for item in raw:
            name = item.get("name", item.get("categoryName", ""))
            score = float(item.get("score", item.get("totalScore", 0)))
            if "思想成长" in name:
                thought = score
            elif "专业技能" in name:
                skill = score
            elif "职业技能" in name:
                career = score
    elif isinstance(raw, dict):
        thought = float(raw.get("thoughtScore", raw.get("sxcz", 0)))
        skill = float(raw.get("skillScore", raw.get("zyjn", 0)))
        career = float(raw.get("careerScore", raw.get("zyjn2", 0)))
    return {"thought_score": thought, "skill_score": skill, "career_score": career}


def fetch_total_scores_with_session(sess: requests.Session) -> dict:
    """获取全部学年总积分（POST 无 body）。"""
    r = sess.post(
        f"{BASE_URL}/Student/My/myScoreTotalGetData.html",
        data={},
        timeout=15,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/Student/My/myScoreTotal.html?ret=",
        },
    )
    r.raise_for_status()
    data = _safe_json_response(r, "总积分")

    log.debug("二课总积分原始响应: %s", str(data)[:500])
    total_score = 0.0
    if isinstance(data, list):
        for item in data:
            total_score += float(item.get("score") or item.get("totalScore") or 0)
    elif isinstance(data, dict):
        total_score = float(
            data.get("score")
            or data.get("totalScore")
            or data.get("totalScoreSum")
            or 0
        )

    log.info("二课总积分: %s", total_score)
    return {"total_score": total_score, "raw": data}


def fetch_total_scores(ssid: str) -> dict:
    return fetch_total_scores_with_session(_session(ssid))


def fetch_module_scores_with_session(
    session: requests.Session, module_id: str, year_id: str = "", term_id: str = ""
) -> dict:
    """
    按模块ID获取分类积分数据
    :param module_id: 模块ID，2=思想政治，4=实践美育类（劳动/文艺/志愿）
    :param year_id: 学年ID，如20252026，为空则获取全部学年
    :param term_id: 学期ID，为空则获取整个学年
    """
    url = "https://2class.cqtbi.edu.cn/Student/My/myScoreTotalGetDataByModuleID.html"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://2class.cqtbi.edu.cn/Student/My/myScoreTotal.html?ret=",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://2class.cqtbi.edu.cn",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    data = {"yearID": year_id, "termID": term_id, "moduleID": module_id}
    response = session.post(url, headers=headers, data=data, timeout=10)
    response.raise_for_status()
    raw_data = _safe_json_response(response, f"模块{module_id}积分")

    # 解析返回数据，按分类汇总
    result = {
        "ideology_score": 0.0,
        "labor_score": 0.0,
        "art_score": 0.0,
        "volunteer_score": 0.0,
        "raw": raw_data,
    }

    for item in raw_data:
        name = item.get("name", "")
        score = float(item.get("score", 0))

        # 分类匹配
        if module_id == "2":  # 思想政治类
            result["ideology_score"] += score
        elif module_id == "4":  # 实践美育类
            if "劳动" in name or "劳动教育" in name:
                result["labor_score"] += score
            elif "文艺" in name or "美育" in name:
                result["art_score"] += score
            elif "志愿" in name or "志愿服务" in name:
                result["volunteer_score"] += score

    return result


def fetch_semester_scores_with_session(
    sess: requests.Session, year_id: str = "20252026"
) -> dict:
    """获取当前学期各分类积分（思想成长/专业技能/职业技能）。"""
    body = {"yearID": year_id, "termID": ""}
    log.debug("二课学期积分请求: year_id=%s body=%s", year_id, body)
    r = sess.post(
        f"{BASE_URL}/Student/My/myScoreTotalGetData.html",
        data=body,
        timeout=15,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": f"{BASE_URL}/Student/My/myScoreTotal.html?ret=",
        },
    )
    r.raise_for_status()
    raw = _safe_json_response(r, "学期积分")

    log.debug("二课学期积分原始响应: %s", str(raw)[:500])
    result = _extract_category_from_raw(raw)

    if not any(result.values()):
        log.warning("二课学期积分全为零，原始响应(前500字符): %s", str(raw)[:500])

    log.info("二课学期积分: %s", result)
    return result


def fetch_all_year_scores_with_session(sess: requests.Session) -> dict:
    """获取全部学年各分类总积分（思想成长/专业技能/职业技能）。"""
    r = sess.post(
        f"{BASE_URL}/Student/My/myScoreTotalGetData.html",
        data={},
        timeout=15,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/Student/My/myScoreTotal.html?ret=",
        },
    )
    r.raise_for_status()
    raw = _safe_json_response(r, "全学年总积分")

    log.debug("二课全学年总积分原始响应: %s", str(raw)[:500])
    category_scores = _extract_category_from_raw(raw)

    # 添加_all后缀，区分学期积分
    result = {
        "total_score_all": 0.0,
        "thought_score_all": category_scores["thought_score"],
        "skill_score_all": category_scores["skill_score"],
        "career_score_all": category_scores["career_score"],
    }

    # 计算总积分
    if isinstance(raw, list):
        for item in raw:
            result["total_score_all"] += float(
                item.get("score") or item.get("totalScore") or 0
            )
    elif isinstance(raw, dict):
        result["total_score_all"] = float(
            raw.get("score") or raw.get("totalScore") or raw.get("totalScoreSum") or 0
        )

    log.info("二课全学年总积分: %s", result)
    return result


def fetch_score_data_json(
    sess: requests.Session, year_term: str = "20252026-2"
) -> dict:
    """获取详细积分数据（getScoreDataJson 接口）。

    返回: {
        "student": {...},  # 学生信息
        "scoreTotal": float, "scoreTotalLimit": float,
        "modules": [{id, name, zf, avg, ...}],
        "MaxScore": float, "hoursTotal": float,
    }
    """
    r = sess.post(
        f"{BASE_URL}/Student/My/getScoreDataJson.html",
        data={"yearTerm": year_term},
        timeout=15,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/Student/My/myScore.html",
        },
    )
    r.raise_for_status()
    data = _safe_json_response(r, "getScoreDataJson")
    log.info(
        "getScoreDataJson 响应: scoreTotal=%s, modules=%s",
        data.get("scoreTotal"),
        len(data.get("modules", [])),
    )
    return data


def fetch_semester_scores(ssid: str, year_id: str = "20252026") -> dict:
    return fetch_semester_scores_with_session(_session(ssid), year_id=year_id)


def fetch_all_with_session(
    sess: requests.Session,
    *,
    student_id: str = "",
    realname: str = "",
    deptname: str = "",
    college: str = "",
    major: str = "",
    year_id: str | None = None,
) -> dict:
    """一站式获取二课全部信息。

    college/major 参数为备用值；页面若有身份信息则优先使用页面值。
    year_id 不传则自动计算当前学年 ID。
    """
    if year_id is None:
        year_id = get_current_year_id()
    counts = fetch_index_counts_with_session(sess)
    total = fetch_total_scores_with_session(sess)
    sem = fetch_semester_scores_with_session(sess, year_id=year_id)
    all_year = fetch_all_year_scores_with_session(sess)

    # 新增：获取当前学期四类积分
    module2_current = fetch_module_scores_with_session(sess, "2", year_id=year_id)
    module4_current = fetch_module_scores_with_session(sess, "4", year_id=year_id)

    # 新增：获取全学年四类积分
    module2_all = fetch_module_scores_with_session(sess, "2", year_id="")
    module4_all = fetch_module_scores_with_session(sess, "4", year_id="")

    # 新增：提取累计时长
    index_html = sess.get(f"{BASE_URL}/Student/My/index.html", timeout=10).text
    total_duration = extract_duration_from_index(index_html)

    data: dict = {
        "student_id": student_id,
        "realname": realname,
        "deptname": deptname,
        "year_id": year_id,
        # 页面解析的身份信息优先，参数值作为回退
        "college": counts.get("college") or college,
        "major": counts.get("major") or major,
        **{k: v for k, v in counts.items() if k not in ("college", "major")},
        **total,
        **sem,
        **all_year,
        # 当前学期新分类积分
        "ideology_score": module2_current["ideology_score"],
        "labor_score": module4_current["labor_score"],
        "art_score": module4_current["art_score"],
        "volunteer_score": module4_current["volunteer_score"],
        # 全学年新分类积分
        "ideology_score_all": module2_all["ideology_score"],
        "labor_score_all": module4_all["labor_score"],
        "art_score_all": module4_all["art_score"],
        "volunteer_score_all": module4_all["volunteer_score"],
        # 时长数据
        "total_duration": total_duration,
        "total_duration_all": total_duration,  # 总时长默认累计，需要时可以区分
        # 分类时长默认0，后续可以扩展从其他接口获取
        "ideology_duration": 0.0,
        "labor_duration": 0.0,
        "art_duration": 0.0,
        "volunteer_duration": 0.0,
        "ideology_duration_all": 0.0,
        "labor_duration_all": 0.0,
        "art_duration_all": 0.0,
        "volunteer_duration_all": 0.0,
        "semester_info": "",
    }
    data.pop("raw", None)
    log.info(
        "二课最终数据: activity=%s unsigned=%s unfinished=%s club=%s "
        "total_score=%s thought=%s skill=%s career=%s "
        "total_score_all=%s thought_all=%s skill_all=%s career_all=%s "
        "ideology=%s labor=%s art=%s volunteer=%s duration=%s",
        data.get("activity_count"),
        data.get("unsigned_count"),
        data.get("unfinished_count"),
        data.get("club_count"),
        data.get("total_score"),
        data.get("thought_score"),
        data.get("skill_score"),
        data.get("career_score"),
        data.get("total_score_all"),
        data.get("thought_score_all"),
        data.get("skill_score_all"),
        data.get("career_score_all"),
        data.get("ideology_score"),
        data.get("labor_score"),
        data.get("art_score"),
        data.get("volunteer_score"),
        data.get("total_duration"),
    )
    return data


def fetch_all(
    ssid: str,
    student_id: str = "",
    realname: str = "",
    deptname: str = "",
    college_major: str = "",
    college: str = "",
    major: str = "",
) -> dict:
    if college_major and not (college or major):
        college = college_major
    return fetch_all_with_session(
        _session(ssid),
        student_id=student_id,
        realname=realname,
        deptname=deptname,
        college=college,
        major=major,
    )


def fetch_and_save_secondclass_info(
    user_id: int, user: dict, *, reply_func=None
) -> dict:
    """统一入口：获取二课信息 → 存入 DB → 返回摘要数据。

    参数：
        user_id: QQ 号（作为参考值存入 qq 列）
        user: 用户凭证字典（含 portal_ticket, access_token, expires_at, student_id 等）
        reply_func: 可选回调 reply_func(msg_type, group_id, user_id, text)，
                    用于发送中间/错误消息。不传则静默处理。

    返回：二课数据字典（用于 format_secondclass_summary）。

    异常：SecondClassAuthError — 认证失败，调用方应捕获并回复用户。
    """
    sess = obtain_secondclass_session_from_user(user)
    data = fetch_all_with_session(
        sess,
        student_id=user.get("student_id", ""),
        realname=user.get("realname", ""),
        deptname=user.get("dept_name") or user.get("depaname", ""),
        college=user.get("college", ""),
        major=user.get("major", ""),
    )
    student_id = data.get("student_id") or user.get("student_id", "")
    data["student_id"] = student_id
    SecondClassDB().upsert(qq=user_id, **data)
    log.info("二课信息查询成功: student_id=%s qq=%s", student_id, user_id)
    return data


# ════════════════════ 活动列表 ════════════════════

# 二课首页活动列表 API
ACTIVITY_CAN_APPLY_URL = f"{BASE_URL}/Student/Activity/getActivityCanApply.html"
ACTIVITY_DETAIL_URL = f"{BASE_URL}/Student/Activity/apply.html"
ACTIVITY_VERIFYCODE_URL = f"{BASE_URL}/Student/Activity/verifycode.html"
ACTIVITY_APPLY_GO_URL = f"{BASE_URL}/Student/Activity/applyGo.html"

# 活动状态码映射
ACTIVITY_STATUS_MAP = {
    "0": "草稿",
    "1": "待审核",
    "2": "审核未通过",
    "3": "报名中",
    "4": "报名结束",
    "5": "活动中",
    "6": "已结束",
    "7": "已取消",
}


def fetch_activities_can_apply(
    sess: requests.Session,
    *,
    module_id: str = "",
    type_id: str = "",
    keywords: str = "",
    sort_by_time: str = "",
    sort_by_score: str = "",
    max_results: int = 10,
) -> list[dict]:
    """获取首页可报名活动列表。

    参数：
        module_id: 模块ID（筛选）
        type_id: 类型ID（筛选）
        keywords: 关键词搜索
        sort_by_time: 按时间排序（如 desc）
        sort_by_score: 按积分排序（如 desc）
        max_results: 最多返回活动数，默认10个

    返回：活动字典列表，每个字段见 _parse_activity_entry。
    """
    body = {
        "moduleID": module_id,
        "typeID": type_id,
        "keywords": keywords,
        "sortByTime": sort_by_time,
        "sortByScore": sort_by_score,
    }
    r = sess.post(
        ACTIVITY_CAN_APPLY_URL,
        data=body,
        timeout=15,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": f"{BASE_URL}/Student/Activity/index.html",
        },
    )
    r.raise_for_status()
    raw = _safe_json_response(r, "可报名活动列表")

    # 解析响应数据
    activities = _parse_activity_list_response(raw)
    log.info("二课可报名活动: 共 %d 个", len(activities))
    return activities[:max_results]


def _parse_activity_list_response(raw: object) -> list[dict]:
    """解析活动列表响应，兼容多种响应格式。"""
    if isinstance(raw, list):
        # 格式1: [maxID, [activity, ...]]
        if len(raw) >= 2 and isinstance(raw[1], list):
            return [_parse_activity_entry(item) for item in raw[1]]
        # 格式2: [activity, ...]
        return [_parse_activity_entry(item) for item in raw]

    if isinstance(raw, dict):
        # 格式3: {rows: [...], total: N}
        rows = raw.get("rows") or raw.get("data") or raw.get("list") or []
        if isinstance(rows, list):
            return [_parse_activity_entry(item) for item in rows]
        # 格式4: {total: N, page: N, data: [...]}
        data = raw.get("data") or raw.get("result") or []
        if isinstance(data, list) and data:
            return [_parse_activity_entry(item) for item in data]

    log.warning("无法识别活动列表响应格式: %s", str(raw)[:300])
    return []


def _parse_activity_entry(item: dict) -> dict:
    """解析单个活动条目，提取关键字段。

    字段说明（从 getActivityCanApply.html JSON 反编译）：
      activityID      活动 ID
      activityName    活动名称
      moduleName      模块名称（思想成长与价值引领 / 职业精神与素质养成等）
      moduleID        模块 ID
      score           可获得积分
      organizerName   主办方名称
      organizerType   主办方类型
      organizerID     主办方 ID
      startDate       活动开始时间（Unix 时间戳）
      endDate         活动结束时间（Unix 时间戳）
      applyStartDate  报名开始时间（Unix 时间戳）
      applyEndDate    报名结束时间（Unix 时间戳）
      status          状态码（0=草稿 1=待审核 2=审核未通过 3=报名中 4=报名结束 5=活动中 6=已结束 7=已取消）
      status2         二级状态码
      status2Name     二级状态名称（如"报名中"）
      img             活动图片文件名
      checkAfterApply 报名后是否需要审核（0=否, 1=是, 2=抽签）
      limitCollege    限制学院（空=全校）
      limitGrade      限制年级
      limitScope      限制范围（0=全校, 1=仅本学院）
      typeID          活动类型 ID
      isClosed        是否已取消（0=否, 1=是）
      summaryRequired 是否需要提交总结（0=否, 1=是）
    """
    start_ts = item.get("startDate") or 0
    end_ts = item.get("endDate") or 0
    apply_start = item.get("applyStartDate") or 0
    apply_end = item.get("applyEndDate") or 0

    return {
        "activity_id": str(item.get("activityID") or item.get("id") or ""),
        "activity_name": str(item.get("activityName") or item.get("name") or ""),
        "module_name": str(item.get("moduleName") or item.get("module_name") or ""),
        "module_id": str(item.get("moduleID") or ""),
        "score": float(item.get("score") or 0),
        "organizer": str(item.get("organizerName") or item.get("organizer") or ""),
        "start_date": _format_timestamp(start_ts),
        "end_date": _format_timestamp(end_ts),
        "apply_start": _format_timestamp(apply_start),
        "apply_end": _format_timestamp(apply_end),
        "status_code": str(item.get("status") or item.get("statusID") or ""),
        "status_name": str(item.get("status2Name", ""))
        or ACTIVITY_STATUS_MAP.get(str(item.get("status", "")), ""),
        "check_after_apply": str(item.get("checkAfterApply", "0")),
        "limit_college": str(item.get("limitCollege", "")),
        "limit_grade": str(item.get("limitGrade", "")),
        "img": str(item.get("img") or ""),
        "is_closed": str(item.get("isClosed", "0")),
    }


def fetch_activity_detail_page(sess: requests.Session, activity_id: str) -> dict | None:
    """获取单个活动详情页面（apply.html），全面提取详细信息。

    实际页面结构为标签与值在相邻独立元素中（非冒号分隔），
    此函数通过标签→值相邻匹配 + 文本正则回退 双策略解析。

    返回字典包含字段：
        activity_id, activity_name, module_name, organizer, score,
        start_date, end_date, apply_time, status_name,
        overview, location, duration, need_sign_out, need_summary

    如果页面无法访问或解析失败，返回 None。
    """
    r = sess.get(
        ACTIVITY_DETAIL_URL,
        params={
            "activityID": activity_id,
            "retUrl": "/Student/Activity/index.html",
        },
        timeout=15,
        headers={
            "Referer": f"{BASE_URL}/Student/Activity/index.html",
        },
    )
    if r.status_code != 200:
        log.warning("活动详情页 %s 返回 %d", activity_id, r.status_code)
        return None
    # 检测"活动不存在"/"跳转提示"/无权访问 提示页
    if any(
        pat in r.text
        for pat in (
            "活动不存在",
            "信息不存在",
            "跳转提示",
            "不是本学院",
            "无权",
            "无权限",
        )
    ):
        log.warning("活动详情 %s 不存在或无权访问", activity_id)
        return None

    raw_html = r.text  # 保留原始 HTML 用于调试
    soup = BeautifulSoup(raw_html, "html.parser")
    title_tag = soup.find("title")
    detail: dict = {
        "activity_id": activity_id,
        "activity_name": "",
        "module_name": "",
        "category_name": "",
        "implementation_method": "",
        "organizer": "",
        "contact_phone": "",
        "score_detail": "",
        "signup_method": "",
        "max_participants": "",
        "current_participants": "",
        "limit_college": "",
        "limit_grade": "",
        "duration": "",
        "cancel_time_limit": "",
        "need_sign_out": "",
        "need_summary": "",
        "organizer_need_summary": "",
        "location_sign": "",
        "attachment": "",
        "main_image": "",
        "start_date": "",
        "end_date": "",
        "apply_time": "",
        "activity_time": "",
        "status_name": "",
        "overview": "",
        "location": "",
    }

    # ── 策略A：标签→值相邻匹配（支持分行时间拼接） ──
    _parse_detail_by_adjacent_labels(soup, detail)

    # ── 策略B：从页面纯文本正则回退（补漏） ──
    page_text = soup.get_text(separator="\n", strip=True)
    _fallback_regex_parse(page_text, detail)

    # ── 修复module_name：如果没有正确提取，回退 ──
    if not detail.get("module_name"):
        detail["module_name"] = "未知分类"

    # ── 活动名称：优先取 title（页面主标题） ──
    if not detail.get("activity_name") and title_tag:
        title_text = title_tag.get_text(strip=True)
        if title_text and len(title_text) >= 4 and "报名" not in title_text:
            detail["activity_name"] = title_text

    # ── 提取概述/实施内容与方式 ──
    _extract_overview(soup, detail)

    # ── 策略C：BeautifulSoup 结构级提取（补漏） ──
    # 主图
    main_img = soup.select_one("div.details img[src*='/upload/']")
    if main_img:
        src = main_img.get("src", "")
        if src:
            detail["main_image"] = src if src.startswith("http") else f"{BASE_URL}{src}"

    # 活动状态（从 CSS class 提取）
    status_el = soup.select_one("span.mui-btn-danger, span.btn.red, span.baom")
    if status_el:
        detail["status_name"] = status_el.get_text(strip=True)

    # 从 <ul class="mui-table-view"> 逐行提取（结构固定：<span>VALUE</span> + 文本LABEL）
    label_field_lookup = {label: field for label, field in _LABEL_FIELD_MAP}
    for li in soup.select("ul.mui-table-view li.mui-table-view-cell"):
        value_span = li.find("span", class_="mui-pull-right")
        if not value_span:
            continue
        # 取 <i> 后面的纯文本（标签）
        i_tag = li.find("i")
        if not i_tag:
            continue
        label_text = i_tag.get_text(strip=True) if i_tag else ""
        if not label_text:
            # 兜底：取所有文本去掉 <i> 内容
            for child in li.children:
                if isinstance(child, str) and child.strip():
                    label_text = child.strip()
                    break
        if not label_text:
            continue
        value = value_span.get_text(strip=True)
        field_name = label_field_lookup.get(label_text)
        if field_name and field_name not in (
            "activity_name",
            "activity_id",
            "overview_base",
            "type_name",
            "apply_time",
            "activity_time",
        ):
            if not detail.get(field_name):
                detail[field_name] = value

    # 活动名称：从 <b class="mui-pull-left"> 提取
    name_el = soup.select_one("b.mui-pull-left, div.mui-content-padded b")
    if name_el and not detail.get("activity_name"):
        detail["activity_name"] = name_el.get_text(strip=True)

    # 主办方+联系电话：从活动详情上方区域提取
    org_div = soup.select_one("div.mui-content-padded div[style*='font-size:16px']")
    if org_div:
        org_text = org_div.get_text(strip=True)
        if "主办方" in org_text:
            org_val = org_text.replace("主办方：", "").split("联系电话")[0].strip()
            if not detail.get("organizer"):
                detail["organizer"] = org_val
        phone_el = org_div.select_one("div[style*='color:#999']")
        if phone_el and not detail.get("contact_phone"):
            detail["contact_phone"] = phone_el.get_text(strip=True).replace(
                "联系电话：", ""
            )

    # module_name 回退：没提取到就用 category_name
    if not detail.get("module_name") and detail.get("category_name"):
        detail["module_name"] = detail["category_name"]

    log.info(
        "二课活动详情: id=%s name=%s module=%s loc=%s signout=%s "
        "score=%s category=%s signup=%s max=%s loc_sign=%s",
        activity_id,
        detail.get("activity_name", ""),
        detail.get("module_name", ""),
        detail.get("location", ""),
        detail.get("need_sign_out", ""),
        detail.get("score_detail", ""),
        detail.get("category_name", ""),
        detail.get("signup_method", ""),
        detail.get("max_participants", ""),
        detail.get("location_sign", ""),
    )
    detail.pop("score_raw", None)  # 清理临时字段

    # 调试：如果活动名称为空，附加原始 HTML 片段到返回结果
    if not detail.get("activity_name", "").strip():
        detail["_raw_html"] = raw_html

    return detail


def _log_debug_html(detail: dict, activity_id: str) -> None:
    """调试辅助：当活动名称为空时，打印原始 HTML 片段。"""
    raw_html = detail.get("_raw_html", "")
    if raw_html:
        # 取前 3000 字符，避免日志爆炸
        snippet = raw_html[:3000]
        log.debug("===== 活动详情名称为空 (id=%s) 原始 HTML 片段 =====", activity_id)
        for i, line in enumerate(snippet.splitlines()[:60], 1):
            log.debug("  %4d: %s", i, line)
        log.debug("===== HTML 结束 (共 %d 行, 截断前 %d 字符) =====",
                   raw_html.count("\n"), len(raw_html))
    else:
        log.debug("活动详情名称为空 (id=%s)，但未获取到原始 HTML（可能已被其他代码路径清理）", activity_id)


def fetch_and_save_activity_detail(
    sess: requests.Session, student_id: str, activity_id: str, qq: int = 0
) -> dict | None:
    """获取活动详情并保存到新 v3 数据库。

    如果活动名称为空，记录错误日志并删除相关记录。
    """
    try:
        detail = fetch_activity_detail_page(sess, activity_id)
        if not detail:
            return None

        act_name = detail.get("activity_name", "").strip()
        if not act_name:
            log.error("活动详情名称为空，跳过（保留 master 记录）: activity_id=%s", activity_id)
            # 调试：打印原始 HTML 片段
            _log_debug_html(detail, activity_id)
            detail_db = SecondClassActivityDetailDB()
            # 只清理无效的 detail 记录，不删除 master
            detail_db.delete_by_activity_id(activity_id)
            return None

        detail_db = SecondClassActivityDetailDB()
        detail_db.upsert(
            activity_id=activity_id,
            activity_name=act_name,
            module_name=detail.get("module_name", ""),
            category_name=detail.get("category_name", ""),
            implementation_method=detail.get("implementation_method", ""),
            overview=detail.get("overview", ""),
            score_detail=detail.get("score_detail", ""),
            organizer=detail.get("organizer", ""),
            contact_phone=detail.get("contact_phone", ""),
            location=detail.get("location", ""),
            duration=detail.get("duration", ""),
            signup_method=detail.get("signup_method", ""),
            max_participants=detail.get("max_participants", ""),
            current_participants=detail.get("current_participants", ""),
            limit_college=detail.get("limit_college", ""),
            limit_grade=detail.get("limit_grade", ""),
            need_sign_out=detail.get("need_sign_out", ""),
            need_summary=detail.get("need_summary", ""),
            organizer_need_summary=detail.get("organizer_need_summary", ""),
            cancel_time_limit=detail.get("cancel_time_limit", ""),
            location_sign=detail.get("location_sign", ""),
            attachment=detail.get("attachment", ""),
            main_image=detail.get("main_image", ""),
            apply_time=detail.get("apply_time", ""),
            activity_time=detail.get("activity_time", ""),
        )

        log.info(
            "活动详情已保存: activity_id=%s student_id=%s", activity_id, student_id
        )
        return detail
    except Exception as e:
        log.error("获取或保存活动详情失败: %s", e)
        return None


def fetch_activity_detail(sess: requests.Session, activity_id: str) -> dict | None:
    """兼容旧接口的活动详情获取函数。"""
    return fetch_activity_detail_page(sess, activity_id)


# ── 已知标签 → 字段名映射（用于相邻元素匹配） ──
_LABEL_FIELD_MAP: list[tuple[str, str]] = [
    ("活动名称", "activity_name"),
    ("模块名称", "module_name"),
    ("类别名称", "type_name"),
    ("实施内容与方式", "overview_base"),
    ("活动积分", "score_detail"),
    ("报名时间", "apply_time"),
    ("活动时间", "activity_time"),
    ("活动地点", "location"),
    ("报名方式", "signup_method"),
    ("限制活动参与人数", "max_participants"),
    ("报名人数", "current_participants"),
    ("限制人员", "limit_college"),
    ("限制年级", "limit_grade"),
    ("发放时长", "duration"),
    ("取消报名时间限制", "cancel_time_limit"),
    ("活动签退", "need_sign_out"),
    ("需要提交总结", "need_summary"),
    ("主办方需要提交总结", "organizer_need_summary"),
    ("开启定位签到", "location_sign"),
    ("活动附件", "attachment"),
    ("主办方", "organizer"),
    ("联系电话", "contact_phone"),
]


def _parse_detail_by_adjacent_labels(soup: BeautifulSoup, detail: dict) -> None:
    """从页面按标签→值相邻匹配提取字段（支持分行时间拼接）。"""
    # 获取所有纯文本片段（适配分行时间格式）
    page_text = soup.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in page_text.split("\n") if line.strip()]

    temp_type = ""
    temp_overview = ""

    # 遍历文本行，精准匹配标签+值（支持分行时间）
    for i in range(len(lines)):
        line = lines[i]
        # 匹配标签
        for label, field in _LABEL_FIELD_MAP:
            if label == line:
                # 处理 报名时间/活动时间（分行拼接）
                if field in ("apply_time", "activity_time"):
                    time_str = ""
                    # 向上查找两行：适配 时间+至+时间 分行格式
                    if i >= 2:
                        time_str = f"{lines[i - 2]} {lines[i - 1]}".replace("至", " - ")
                    elif i >= 1:
                        time_str = lines[i - 1]
                    detail[field] = time_str
                # 处理普通字段：值在标签前一行，所以i≥1且下一行不是其他标签
                elif i >= 1 and not any(k in lines[i - 1] for k, _ in _LABEL_FIELD_MAP):
                    detail[field] = lines[i - 1]
                # 类别名称 → category_name（值在上一行）
                if field == "type_name":
                    temp_type = lines[i - 1] if i >= 1 else ""
                # 实施内容与方式 → implementation_method（值在上一行）
                if field == "overview_base":
                    temp_overview = lines[i - 1] if i >= 1 else ""

    # 类别名称写入 category_name（不覆盖 module_name）
    if temp_type:
        detail["category_name"] = temp_type

    # 实施内容与方式写入 implementation_method（不覆盖 overview）
    if temp_overview:
        detail["implementation_method"] = temp_overview

    # 提取"活动详情"段落文本追加到 overview（不改）
    detail_text_parts = []
    in_detail = False
    for line in lines:
        if "活动详情" in line and len(line) <= 8:
            in_detail = True
            continue
        if in_detail:
            # 遇到下一个已知标签则停止
            if any(k in line for k, _ in _LABEL_FIELD_MAP):
                break
            detail_text_parts.append(line)
    if detail_text_parts:
        detail_text = "\n".join(detail_text_parts)
        if detail.get("overview"):
            detail["overview"] = detail["overview"] + "\n---\n" + detail_text
        else:
            detail["overview"] = detail_text


def _fallback_regex_parse(page_text: str, detail: dict) -> None:
    """正则回退解析：从纯文本中提取未被相邻匹配覆盖的字段（含分行时间兜底）。"""
    # 1. 活动名称
    if not detail.get("activity_name"):
        m = re.search(r"^(.+?)\n报名中", page_text)
        if m:
            detail["activity_name"] = m.group(1).strip()
        else:
            m = re.search(r"活动名称[：:]\s*(.{2,40}?)(?=活动|$)", page_text)
            if m:
                detail["activity_name"] = m.group(1).strip()

    # 2. 报名时间（精准匹配分行格式）
    if not detail.get("apply_time"):
        m = re.search(
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\n\s*至\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*报名时间",
            page_text,
        )
        if m:
            detail["apply_time"] = f"{m.group(1)} {m.group(2)}".replace(" 至", " - ")

    # 3. 活动时间（精准匹配分行格式）
    if not detail.get("activity_time"):
        m = re.search(
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\n\s*至\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*活动时间",
            page_text,
        )
        if m:
            detail["activity_time"] = f"{m.group(1)} {m.group(2)}".replace(" 至", " - ")

    # 4. 活动地点
    if not detail.get("location"):
        m = re.search(r"(?:合川|渝北|巴南|大学城|华岩|校区)", page_text)
        if m:
            detail["location"] = m.group().strip()

    # 5. 发放时长
    if not detail.get("duration"):
        m = re.search(r"(\d+\.?\d*)\s*发放时长", page_text)
        if m:
            detail["duration"] = f"{m.group(1)}小时"
        elif not detail.get("duration"):
            m = re.search(r"(?:不发放|(\d+\.?\d*)小时)", page_text)
            if m:
                detail["duration"] = m.group(0)

    # 6. 活动签退
    if not detail.get("need_sign_out"):
        m = re.search(r"(是|否)\s*活动签退", page_text)
        if m:
            detail["need_sign_out"] = f"{m.group(1)}签退"

    # 7. 需要提交总结
    if not detail.get("need_summary"):
        m = re.search(r"(是|否)\s*需要提交总结", page_text)
        if m:
            detail["need_summary"] = (
                "需要提交总结" if m.group(1) == "是" else "无需提交总结"
            )


def _extract_overview(soup: BeautifulSoup, detail: dict) -> None:
    """从页面中提取概述/实施内容与方式。"""
    # 如果 overview 已被 _parse_detail_by_adjacent_labels 设置，直接跳过
    if detail.get("overview"):
        return
    # 策略1：找 <textarea> 或含"概述"、"实施内容"、"活动内容"的标签
    overview_keywords = [
        "概述",
        "实施内容",
        "实施方式",
        "活动内容",
        "活动介绍",
        "内容与方式",
        "活动概述",
        "实施内容与方式",
    ]

    # 查找带有这些关键词的标签
    for tag in soup.find_all(
        ["div", "p", "span", "td", "th", "label", "h3", "h4", "li"]
    ):
        text = tag.get_text(strip=True)
        for kw in overview_keywords:
            if kw in text:
                # 提取标签后的兄弟节点
                nxt = tag.find_next_sibling()
                if nxt and nxt.get_text(strip=True):
                    val = nxt.get_text(strip=True)
                    if len(val) > 5:
                        detail["overview"] = val
                        return
                # 同一标签内容中冒号后的部分
                val = _extract_value_after_label(text)
                if val and len(val) > 5:
                    detail["overview"] = val
                    return

    # 策略2：查找包含概述内容的 <textarea>
    textarea = soup.find("textarea")
    if textarea and textarea.get_text(strip=True):
        val = textarea.get_text(strip=True)
        if len(val) > 10:
            detail["overview"] = val
            return

    # 策略3：查找 class 或 id 含 overview/content/description 的元素
    for cls_name in ("overview", "content", "description", "detail", "intro"):
        for el in soup.find_all(class_=re.compile(cls_name, re.I)):
            val = el.get_text(strip=True)
            if len(val) > 10 and not detail["overview"]:
                detail["overview"] = val
                return

    # 策略4：从页面文本中扫描
    page_text = soup.get_text("\n", strip=True)
    ov_m = re.search(
        r"(?:概述|实施内容|活动内容|活动介绍)[：:]\s*([\s\S]*?)(?=\n\S+[：:]|\Z)",
        page_text,
    )
    if ov_m:
        val = ov_m.group(1).strip()
        if len(val) > 5:
            detail["overview"] = val
            return

    # 策略5：从表单中找最大文本块（通常是活动说明）
    form = soup.find("form")
    if form:
        texts = [
            t.get_text(strip=True)
            for t in form.find_all(["div", "p"])
            if len(t.get_text(strip=True)) > 30
        ]
        if texts:
            # 选第一个长度适中的文本作为概述
            for t in texts:
                if 20 < len(t) < 1000:
                    detail["overview"] = t
                    return


# 旧版本兼容别名
fetch_activity_detail = fetch_activity_detail_page


# ════════════════════ 批量拉取活动详情 ════════════════════


def fetch_and_store_all_activity_details(
    sess: requests.Session,
    student_id: str = "",
    *,
    activity_ids: list[str] | None = None,
    max_activities: int = 20,
    include_reparse: bool = False,
) -> dict:
    """批量拉取活动详情并存入 DB。

    如果活动名称为空，记录调度日志错误并删除 master/detail 中的相关记录。

    参数：
        sess: 已认证的二课会话
        student_id: 学号（仅用于过滤 master 表中的活动）
        activity_ids: 指定要拉取的活动 ID 列表（None 则自动从 master 表获取）
        max_activities: 单次最多拉取数量
        include_reparse: 是否重新拉取旧版解析（category_name/implementation_method 为空）的记录

    返回：{"fetched": N, "failed": N, "total": N, "deleted_empty_name": N, "activity_ids": [...]}
    """
    detail_db = SecondClassActivityDetailDB()
    master_db = SecondClassMasterDB()

    # 确定要拉取的活动 ID 列表
    ids_to_fetch: list[str] = []
    if activity_ids:
        ids_to_fetch = activity_ids[:max_activities]
    else:
        # 自动从未拉取过的活动中选择
        ids_to_fetch = detail_db.get_unfetched_activity_ids()[:max_activities]
        # 可选：补充旧版需重解析的记录
        if include_reparse:
            reparse_ids = detail_db.get_need_reparse_activity_ids()
            # 去重合并
            existing = set(ids_to_fetch)
            for rid in reparse_ids:
                if rid not in existing and len(ids_to_fetch) < max_activities:
                    ids_to_fetch.append(rid)
                    existing.add(rid)

    if not ids_to_fetch:
        log.info("活动详情：没有需要拉取的活动 ID")
        return {
            "fetched": 0,
            "failed": 0,
            "total": 0,
            "deleted_empty_name": 0,
            "activity_ids": [],
        }

    log.info("活动详情：开始批量拉取 %d 个活动", len(ids_to_fetch))
    fetched = 0
    failed = 0
    deleted_empty_name = 0
    for aid in ids_to_fetch:
        try:
            detail = fetch_activity_detail_page(sess, aid)
            if detail:
                # 检查活动名称是否为空
                act_name = detail.get("activity_name", "").strip()
                if not act_name:
                    log.error("活动详情名称为空，跳过详情更新（保留 master 记录）: activity_id=%s", aid)
                    # 调试：打印原始 HTML 片段
                    _log_debug_html(detail, aid)
                    # 只清理无效的 detail 记录，不删除 master（master 来自"我的活动"等合法来源）
                    detail_db.delete_by_activity_id(aid)
                    deleted_empty_name += 1
                    failed += 1
                else:
                    detail_db.upsert(
                        activity_id=aid,
                        activity_name=act_name,
                        module_name=detail.get("module_name", ""),
                        category_name=detail.get("category_name", ""),
                        implementation_method=detail.get("implementation_method", ""),
                        overview=detail.get("overview", ""),
                        score_detail=detail.get("score_detail", ""),
                        organizer=detail.get("organizer", ""),
                        contact_phone=detail.get("contact_phone", ""),
                        location=detail.get("location", ""),
                        duration=detail.get("duration", ""),
                        signup_method=detail.get("signup_method", ""),
                        max_participants=detail.get("max_participants", ""),
                        current_participants=detail.get("current_participants", ""),
                        limit_college=detail.get("limit_college", ""),
                        limit_grade=detail.get("limit_grade", ""),
                        need_sign_out=detail.get("need_sign_out", ""),
                        need_summary=detail.get("need_summary", ""),
                        organizer_need_summary=detail.get("organizer_need_summary", ""),
                        cancel_time_limit=detail.get("cancel_time_limit", ""),
                        location_sign=detail.get("location_sign", ""),
                        attachment=detail.get("attachment", ""),
                        main_image=detail.get("main_image", ""),
                        apply_time=detail.get("apply_time", ""),
                        activity_time=detail.get("activity_time", ""),
                    )
                    fetched += 1
                    log.info("活动详情已更新: activity_id=%s name=%s", aid, act_name)
            else:
                failed += 1
        except Exception as e:
            log.warning("活动详情拉取失败: activity_id=%s error=%s", aid, e)
            failed += 1

        # 短暂间隔避免触发反爬
        time.sleep(3)

    stats = {
        "fetched": fetched,
        "failed": failed,
        "total": len(ids_to_fetch),
        "deleted_empty_name": deleted_empty_name,
        "activity_ids": ids_to_fetch,
    }
    log.info("活动详情批量拉取完成: %s", stats)
    return stats


# ════════════════════ 格式化活动详情 ════════════════════


def format_activity_detail(detail: dict) -> str:
    """将活动详情格式化为可读文本。"""
    if not detail:
        return "━━ 活动详情 ━━\n暂无数据"

    lines = [
        "━━ 活动详情 ━━",
        f"📋 活动名称: {detail.get('activity_name', '') or '未获取'}",
    ]

    if detail.get("activity_id"):
        lines.append(f"🆔 活动ID: {detail['activity_id']}")
    if detail.get("status_name"):
        lines.append(f"📌 活动状态: {detail['status_name']}")
    if detail.get("module_name"):
        lines.append(f"🏷️ 类别名称: {detail['module_name']}")
    else:
        lines.append(f"🏷️ 模块名称: {detail.get('module_name', '')}")
    if detail.get("category_name"):
        lines.append(f"📂 活动分类: {detail['category_name']}")
    if detail.get("implementation_method"):
        lines.append(f"📖 实施方式: {detail['implementation_method']}")
    if detail.get("score_detail"):
        lines.append(f"⭐ 活动积分: {detail['score_detail']}")
    if detail.get("location"):
        lines.append(f"📍 活动地点: {detail['location']}")
    if detail.get("duration"):
        lines.append(f"⏱️ 发放时长: {detail['duration']}")
    if detail.get("apply_time"):
        lines.append(f"📥 报名时间: {detail['apply_time']}")
    if detail.get("activity_time"):
        lines.append(f"📅 活动时间: {detail['activity_time']}")
    if detail.get("signup_method"):
        lines.append(f"📋 报名方式: {detail['signup_method']}")
    if detail.get("max_participants"):
        lines.append(f"👥 限制人数: {detail['max_participants']}")
    if detail.get("current_participants"):
        lines.append(f"👤 报名人数: {detail['current_participants']}")
    if detail.get("limit_college"):
        lines.append(f"🏫 限制学院: {detail['limit_college']}")
    if detail.get("limit_grade"):
        lines.append(f"🎓 限制年级: {detail['limit_grade']}")
    if detail.get("organizer"):
        lines.append(f"🏢 主办方: {detail['organizer']}")
    if detail.get("contact_phone"):
        lines.append(f"📞 联系电话: {detail['contact_phone']}")
    if detail.get("cancel_time_limit"):
        lines.append(f"⏰ 取消时间: {detail['cancel_time_limit']}")
    if detail.get("need_sign_out"):
        lines.append(f"🚪 活动签退: {detail['need_sign_out']}")
    if detail.get("need_summary"):
        lines.append(f"📝 需要总结: {detail['need_summary']}")
    if detail.get("organizer_need_summary"):
        lines.append(f"📄 主办方总结: {detail['organizer_need_summary']}")
    if detail.get("location_sign"):
        lines.append(f"📍 定位签到: {detail['location_sign']}")

    if detail.get("overview"):
        overview = detail["overview"]
        if len(overview) > 200:
            overview = overview[:200] + "..."
        lines.append(f"\n📄 活动详情:\n{overview}")

    result = "\n".join(lines)

    # 文本长度限制
    max_len = 800
    if len(result) > max_len:
        result = result[:max_len] + "\n...（余略）"

    return result


def _extract_value_after_label(text: str) -> str:
    """从类似 '活动名称：某某活动' 的文本中提取冒号后的值。"""
    for sep in ["：", ":", ":", " "]:
        if sep in text:
            parts = text.split(sep, 1)
            if len(parts) > 1 and parts[1].strip():
                return parts[1].strip()
    return ""


def _format_timestamp(ts: object) -> str:
    """将 Unix 时间戳转为月-日 时:分 格式（CST 北京时间，UTC+8）。"""
    try:
        ts_int = int(ts)
        from datetime import timezone, timedelta

        # CST = UTC+8
        cst = timezone(timedelta(hours=8))
        dt = datetime.fromtimestamp(ts_int, tz=cst)
        return dt.strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(ts) if ts else ""


def _infer_activity_status(item: dict) -> str:
    """根据活动字段推断状态。"""
    if item.get("isClosed") == 1:
        return "已取消"
    if item.get("applyStatus") == 2:
        return "报名失败"
    status = item.get("status", "")
    if status:
        return str(status)
    return ""


# ════════════════════ 我的活动（所有标签页） ════════════════════

# "我的活动"页面各标签页 API
MY_ACTIVITY_END_URL = f"{BASE_URL}/Student/My/myActivity_End.html"
MY_ACTIVITY_OTHER_URL = f"{BASE_URL}/Student/My/myActivity_other.html"
ACTIVITY_NO_SIGN_URL = f"{BASE_URL}/Student/My/getActivityNoSign.html"
ACTIVITY_NO_SUMMARY_URL = f"{BASE_URL}/Student/My/getActivityNoSummary.html"
ACTIVITY_SUMMARY_URL = f"{BASE_URL}/Student/My/myActivitySummary.html"

# 标签页 ID 映射
MY_ACTIVITY_TABS = {
    "1": "报名中",  # tab1: 报名中
    "2": "活动中",  # tab2: 活动中
    "3": "已结束",  # tab3: 已结束 (AJAX 分页)
    "4": "其它",  # tab4: 其它 (AJAX 分页)
    "5": "未开始",  # tab5: 未开始
}


def fetch_all_my_activities(sess: requests.Session) -> dict[str, list[dict]]:
    """获取 '我的活动' 页面所有标签页的活动。

    返回: { "报名中": [...], "活动中": [...], "已结束": [...], ... }
    """
    # 1. 获取 myActivity.html 页面（内含 tab1,2,5 的服务端渲染数据）
    r = sess.get(f"{BASE_URL}/Student/My/myActivity.html", timeout=15)
    if "top.location.href" in r.text or r.status_code in (301, 302, 303, 307):
        raise SecondClassAuthError("二课登录已过期，请重新 #扫码登录")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    result: dict[str, list[dict]] = {}

    # 2. 解析服务端渲染的标签页（tab1=报名中, tab2=活动中, tab5=未开始）
    for tab_id in ("1", "2", "5"):
        tab_name = MY_ACTIVITY_TABS[tab_id]
        activities = _parse_my_activity_tab_html(soup, tab_id)
        result[tab_name] = activities
        log.info("二课我的活动[%s]: %d 个", tab_name, len(activities))

    # 3. 通过 AJAX 加载 tab3（已结束, 分页）
    result["已结束"] = _fetch_paginated_my_activity(
        sess, MY_ACTIVITY_END_URL, soup, tab_id="3"
    )

    # 4. 通过 AJAX 加载 tab4（其它, 分页）
    result["其它"] = _fetch_paginated_my_activity(
        sess, MY_ACTIVITY_OTHER_URL, soup, tab_id="4"
    )

    total = sum(len(v) for v in result.values())
    log.info("二课所有我的活动: 共 %d 个", total)
    return result


def _parse_my_activity_tab_html(soup: BeautifulSoup, tab_id: str) -> list[dict]:
    """从 myActivity.html 中解析服务端渲染的标签页活动列表。

    tab_id → status 映射：
        1 → 报名中, 2 → 活动中, 5 → 未开始
    """
    # tab 表 → status_code, status_name
    TAB_STATUS: dict[str, tuple[str, str]] = {
        "1": ("3", "报名中"),
        "2": ("5", "活动中"),
        "5": ("0", "未开始"),
    }
    default_status_code, default_status_name = TAB_STATUS.get(tab_id, ("", ""))

    tab_div = soup.find("div", id=f"item{tab_id}")
    if not tab_div:
        return []

    activities: list[dict] = []
    for li in tab_div.find_all("li"):
        a_tag = li.find("a", class_="mui-clearfix") if li.find("a") else li.find("a")
        if not a_tag:
            continue

        href = a_tag.get("href", "")
        act_id = ""
        if "activityID=" in href:
            from urllib.parse import parse_qs, urlparse

            qs = parse_qs(urlparse(href).query)
            act_id = qs.get("activityID", [""])[0]

        # 提取显示文本
        texts = [t.get_text(strip=True) for t in a_tag.find_all(["b", "p", "span"])]
        name = texts[0] if texts else ""

        # 提取模块名
        module_span = a_tag.find("span")
        module_name = module_span.get_text(strip=True) if module_span else ""

        # 提取积分
        score_text = a_tag.get_text("|", strip=True)
        score = 0.0
        for part in score_text.split("|"):
            if "积分" in part:
                try:
                    score = float(part.replace("积分", "").strip())
                except ValueError:
                    pass
                break

        if not act_id and not name:
            continue

        entry = {
            "activity_id": act_id,
            "activity_name": name,
            "module_name": module_name,
            "score": score,
            "organizer": "",
            "start_date": "",
            "end_date": "",
            "status_code": default_status_code,
            "status_name": default_status_name,
        }

        # 从 <p> 标签中提取时间信息
        for p in a_tag.find_all("p"):
            text = p.get_text(strip=True)
            if "活动" in text and "ID" in text:
                pass  # 活动ID行
            elif "开始" in text:
                entry["start_date"] = (
                    text.replace("开始时间：", "").replace("开始时间:", "").strip()
                )
            elif "结束" in text:
                entry["end_date"] = (
                    text.replace("结束时间：", "").replace("结束时间:", "").strip()
                )
            elif "主办方" in text:
                entry["organizer"] = (
                    text.replace("主办方：", "").replace("主办方:", "").strip()
                )

        activities.append(entry)

    return activities


def _fetch_paginated_my_activity(
    sess: requests.Session, url: str, soup: BeautifulSoup, tab_id: str
) -> list[dict]:
    """通过 AJAX 分页 API 获取更多活动。先发第一页，再自动翻页。"""
    tab_div = soup.find("div", id=f"item{tab_id}")
    if not tab_div:
        return []

    # 读取初始分页参数（在页面 hidden input 中）
    p_input = tab_div.find("input", {"id": "p"}) or tab_div.find(
        "input", {"id": f"p{tab_id}"}
    )
    max_input = tab_div.find("input", {"id": "maxActivityID"}) or tab_div.find(
        "input", {"id": f"maxActivityID{tab_id}"}
    )

    page = int(p_input["value"]) if p_input else 1
    max_id = max_input["value"] if max_input else ""

    # tab_id → 默认 (status_code, status_name)
    TAB_DEFAULT_STATUS: dict[str, tuple[str, str]] = {
        "3": ("6", "已结束"),
        "4": ("", "其它"),
    }
    default_code, default_name = TAB_DEFAULT_STATUS.get(tab_id, ("", ""))

    all_activities: list[dict] = []
    max_pages = 5  # 安全限制，最多拉5页

    for _ in range(max_pages):
        body = {"p": str(page), "maxActivityID": max_id}
        if tab_id == "3":  # 已结束多一个 type 参数
            body["type"] = ""

        try:
            r = sess.post(
                url,
                data=body,
                timeout=15,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Referer": f"{BASE_URL}/Student/My/myActivity.html",
                },
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("二课我的活动分页[%s]请求失败: %s", tab_id, e)
            break

        # 响应格式: [maxID, [activities...]]
        if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
            max_id = str(data[0])
            items = data[1]
            if not items:
                break
            for item in items:
                entry = _parse_activity_entry(item)
                # AJAX 响应可能缺失 status 字段，用 tab 默认值补全
                if not entry.get("status_code"):
                    entry["status_code"] = default_code
                if not entry.get("status_name"):
                    entry["status_name"] = default_name
                all_activities.append(entry)
            page += 1
        else:
            break

    return all_activities


# ════════════════════ 未结束活动（报名中+活动中+未开始）拉取 ════════════════════


def fetch_and_store_my_unfinished_activities(
    sess: requests.Session,
    student_id: str,
    qq: int = 0,
) -> dict:
    """拉取用户的未结束活动（报名中+活动中+未开始）并存入独立表。

    返回: { "tab1_count": N, "tab2_count": N, "tab5_count": N, "total": N }
    """
    result: dict = {"tab1_count": 0, "tab2_count": 0, "tab5_count": 0, "total": 0}
    try:
        my_activities = fetch_all_my_activities(sess)
    except Exception as e:
        log.warning("二课未结束活动拉取失败: student_id=%s error=%s", student_id, e)
        return result

    # 只关注"未结束"标签：报名中、活动中、未开始
    unfinished_tabs = ["报名中", "活动中", "未开始"]
    all_unfinished: list[dict] = []
    for tab_name in unfinished_tabs:
        acts = my_activities.get(tab_name, [])
        all_unfinished.extend(acts)

    if not all_unfinished:
        log.info("二课未结束活动: student_id=%s 无未结束活动", student_id)
        return result

    # 写入 old 表（一活动一行）
    db = SecondClassUserActivityDB()
    db.clear_by_student(student_id)
    count = db.upsert_activities(student_id, all_unfinished, qq=qq)

    # 写入 new 表（一学生一行，逗号分隔）
    activity_ids_only = [a.get("activity_id", "") for a in all_unfinished if a.get("activity_id")]
    db.update_unfinished_ids(student_id=student_id, activity_ids=activity_ids_only, qq=qq)

    result["tab1_count"] = len(my_activities.get("报名中", []))
    result["tab2_count"] = len(my_activities.get("活动中", []))
    result["tab5_count"] = len(my_activities.get("未开始", []))
    result["total"] = count

    log.info("二课未结束活动: student_id=%s 共 %d 个 (报名中=%d, 活动中=%d, 未开始=%d)",
             student_id, count, result["tab1_count"], result["tab2_count"], result["tab5_count"])
    return result


# ════════════════════ 二课总表调度 ════════════════════


def get_schedule_interval() -> int:
    """根据当前时间返回下次拉取间隔（秒）。

    规则（由用户指定）：
      周二 06:00-22:00, 周三 06:00-17:00 → 15分钟
      周二到周三其余时间 → 30分钟
      其它日子 → 3小时
    """
    now = datetime.now()
    weekday = now.weekday()  # 0=Mon, 1=Tue, 2=Wed ...
    hour = now.hour
    minute = now.minute
    time_minutes = hour * 60 + minute

    # 周二 (weekday=1): 06:00-22:00 → 15分钟
    if weekday == 1:
        if 360 <= time_minutes < 1320:  # 06:00 ~ 22:00
            return 15 * 60
        else:
            return 30 * 60
    # 周三 (weekday=2): 06:00-17:00 → 15分钟
    if weekday == 2:
        if 360 <= time_minutes < 1020:  # 06:00 ~ 17:00
            return 15 * 60
        else:
            # 周三 17:00 之后到周四 00:00 为 30 分钟
            if time_minutes >= 1020:
                return 30 * 60
            # 周三 00:00-06:00 也 30 分钟
            return 30 * 60

    # 其它日子: 3小时
    return 3 * 3600


def discover_new_activities(
    sess: requests.Session,
    student_id: str,
    qq: int = 0,
    scan_range: int = 50,
    delay: float = 0.3,
) -> dict:
    """扫描发现新活动（通过遍历活动 ID 范围补全未收录的活动）。

    从 master_v2 表中当前最大的 activity_id 开始，向上遍历 scan_range 个 ID，
    尝试拉取详情，成功的活动写入 master_v2 表。

    参数：
        scan_range: 扫描的 ID 数量
        delay: 每次请求间隔（秒），避免触发反爬

    返回: { "discovered": N, "upcoming": N, "total_scanned": N }
    """
    master_db = SecondClassMasterDB()
    detail_db = SecondClassActivityDetailDB()

    # 获取当前已知的最大活动 ID
    conn_max = master_db._connect()
    row = conn_max.execute(
        "SELECT CAST(activity_id AS INTEGER) FROM second_class_master_v2 "
        "WHERE activity_id GLOB '[0-9]*' "
        "ORDER BY CAST(activity_id AS INTEGER) DESC LIMIT 1"
    ).fetchone()
    start_id = (row[0] + 1) if row else 0
    conn_max.close()

    # 获取已有 ID 集合，避免重复
    existing_ids = set()
    conn_exist = master_db._connect()
    for r in conn_exist.execute(
        "SELECT DISTINCT activity_id FROM second_class_master_v2"
    ):
        existing_ids.add(r[0])
    conn_exist.close()

    discovered = 0
    upcoming = 0
    scanned = 0

    log.info(
        "扫描新活动: student_id=%s 起始ID=%d 范围=%d 已有=%d 个",
        student_id, start_id, scan_range, len(existing_ids),
    )

    for aid_int in range(start_id, start_id + scan_range):
        aid = str(aid_int)
        scanned += 1

        # 跳过已存在的活动
        if aid in existing_ids:
            continue

        try:
            detail = fetch_activity_detail_page(sess, aid)
            if not detail:
                time.sleep(delay)
                continue

            act_name = detail.get("activity_name", "").strip()
            if not act_name:
                time.sleep(delay)
                continue

            # 构造活动记录写入 master 表
            activity_record = {
                "activity_id": aid,
                "activity_name": act_name,
                "module_name": detail.get("module_name", ""),
                "score": detail.get("score_detail", "0"),
                "organizer": detail.get("organizer", ""),
                "start_date": detail.get("start_date", ""),
                "end_date": detail.get("end_date", ""),
                "apply_start": detail.get("apply_time", ""),
                "apply_end": "",
                "status_code": "",
                "status_name": detail.get("status_name", ""),
                "check_after_apply": "0",
                "limit_college": detail.get("limit_college", ""),
                "limit_grade": detail.get("limit_grade", ""),
                "img": detail.get("main_image", ""),
                "is_closed": "0",
            }
            master_db.upsert_activities(student_id, [activity_record], qq=qq)
            discovered += 1

            # 也保存详情缓存
            detail_db.upsert(
                activity_id=aid,
                activity_name=act_name,
                module_name=detail.get("module_name", ""),
                category_name=detail.get("category_name", ""),
                implementation_method=detail.get("implementation_method", ""),
                overview=detail.get("overview", ""),
                score_detail=detail.get("score_detail", ""),
                organizer=detail.get("organizer", ""),
                contact_phone=detail.get("contact_phone", ""),
                location=detail.get("location", ""),
                duration=detail.get("duration", ""),
                signup_method=detail.get("signup_method", ""),
                max_participants=detail.get("max_participants", ""),
                current_participants=detail.get("current_participants", ""),
                limit_college=detail.get("limit_college", ""),
                limit_grade=detail.get("limit_grade", ""),
                need_sign_out=detail.get("need_sign_out", ""),
                need_summary=detail.get("need_summary", ""),
                organizer_need_summary=detail.get("organizer_need_summary", ""),
                cancel_time_limit=detail.get("cancel_time_limit", ""),
                location_sign=detail.get("location_sign", ""),
                attachment=detail.get("attachment", ""),
                main_image=detail.get("main_image", ""),
                apply_time=detail.get("apply_time", ""),
                activity_time=detail.get("activity_time", ""),
                start_date=detail.get("start_date", ""),
                end_date=detail.get("end_date", ""),
                status_name=detail.get("status_name", ""),
            )

            log.debug("扫描新活动: 发现 activity_id=%s name=%s", aid, act_name)

            # 判断是否"报名未开始"（仅含"未开始"的状态）
            status = detail.get("status_name", "")
            if "未开始" in status:
                upcoming += 1

            time.sleep(delay)

        except Exception as e:
            log.debug("扫描新活动: activity_id=%s 跳过 (%s)", aid, e)
            time.sleep(delay)
            continue

    result = {
        "discovered": discovered,
        "upcoming": upcoming,
        "total_scanned": scanned,
    }
    log.info(
        "扫描新活动完成: student_id=%s 扫描=%d 发现=%d 报名未开始=%d",
        student_id, scanned, discovered, upcoming,
    )
    return result


def fetch_and_store_master_data(
    sess: requests.Session,
    student_id: str,
    qq: int = 0,
    fetch_details: bool = False,
    max_detail_activities: int = 10,
    include_reparse: bool = False,
    scan_new: bool = False,
    scan_range: int = 50,
) -> dict:
    """一站式拉取所有二课数据并存入总表。

    参数：
        fetch_details: 是否同时拉取活动详情
        max_detail_activities: 单次最多拉取详情活动数（避免请求过多）
        include_reparse: 是否重新拉取旧版解析（category_name 为空）的记录
        scan_new: 是否扫描发现新活动（通过遍历活动 ID 范围）
        scan_range: 扫描的活动 ID 范围大小

    返回: { "can_apply": N, "my_activities": {"报名中":N, ...}, "total_count": N,
            "detail_fetched": N, "discovered": N }
    """
    stats: dict = {}

    # 1. 拉取可报名活动
    try:
        can_apply = fetch_activities_can_apply(sess, max_results=9999)
        master_db = SecondClassMasterDB()
        cnt = master_db.upsert_activities(student_id, can_apply, qq=qq)
        stats["can_apply"] = cnt
        log.info("二课总表: student_id=%s 可报名 %d 个", student_id, cnt)
    except Exception as e:
        log.warning("二课总表: student_id=%s 可报名拉取失败: %s", student_id, e)
        stats["can_apply"] = 0

    # 2. 拉取我的活动（所有标签页）
    try:
        my_activities = fetch_all_my_activities(sess)
        tab_counts = {}
        total_my = 0
        for tab_name, acts in my_activities.items():
            cnt = master_db.upsert_activities(student_id, acts, qq=qq)
            tab_counts[tab_name] = cnt
            total_my += cnt
        stats["my_activities"] = tab_counts
        stats["my_total"] = total_my
        log.info("二课总表: student_id=%s 我的活动 %d 个", student_id, total_my)
    except Exception as e:
        log.warning("二课总表: student_id=%s 我的活动拉取失败: %s", student_id, e)
        stats["my_activities"] = {}
        stats["my_total"] = 0

    stats["total_count"] = stats.get("can_apply", 0) + stats.get("my_total", 0)

    # 3. 可选：拉取活动详情
    if fetch_details:
        try:
            detail_result = fetch_and_store_all_activity_details(
                sess,
                student_id=student_id,
                max_activities=max_detail_activities,
                include_reparse=include_reparse,
            )
            stats["detail_fetched"] = detail_result.get("fetched", 0)
            stats["detail_total"] = detail_result.get("total", 0)
            log.info(
                "二课总表: student_id=%s 活动详情拉取 %d/%d",
                student_id,
                stats["detail_fetched"],
                stats["detail_total"],
            )
        except Exception as e:
            log.warning("二课总表: student_id=%s 活动详情拉取失败: %s", student_id, e)
            stats["detail_fetched"] = 0
            stats["detail_total"] = 0
    else:
        stats["detail_fetched"] = 0
        stats["detail_total"] = 0

    # 4. 可选：扫描发现新活动
    if scan_new and scan_range > 0:
        try:
            scan_result = discover_new_activities(
                sess, student_id=student_id, qq=qq,
                scan_range=scan_range, delay=0.5,
            )
            stats["discovered"] = scan_result.get("discovered", 0)
            stats["upcoming"] = scan_result.get("upcoming", 0)
            log.info(
                "二课总表: student_id=%s 扫描新活动 %d 个 (报名未开始 %d)",
                student_id, stats["discovered"], stats["upcoming"],
            )
        except Exception as e:
            log.warning("二课总表: student_id=%s 扫描新活动失败: %s", student_id, e)
            stats["discovered"] = 0
            stats["upcoming"] = 0
    else:
        stats["discovered"] = 0
        stats["upcoming"] = 0

    return stats


def get_all_user_credentials() -> list[dict]:
    """获取所有用户的凭证信息（从 accounts.json 和 users.db 的 users 表）。

    查找顺序：
      1. users 表 — 扫码登录用户（有 qq + access_token）
      2. accounts.json — 密码登录用户（有 student_id + token）

    去重：同 student_id 只保留一条（accounts.json 覆盖 users 表）。
    """
    seen: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(str(USER_DB_FILE), timeout=5)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "users" in tables:
            rows = conn.execute(
                "SELECT qq, student_id, realname, access_token, "
                "portal_ticket, expires_at FROM users "
                "WHERE (access_token != '' AND access_token IS NOT NULL) "
                "   OR (portal_ticket != '' AND portal_ticket IS NOT NULL)"
            ).fetchall()
            for r in rows:
                sid = (r[1] or "").strip()
                if sid:
                    seen[sid] = {
                        "qq": r[0],
                        "student_id": sid,
                        "realname": r[2] or "",
                        "portal_ticket": r[4] or "",
                        "access_token": r[3] or "",
                        "expires_at": r[5] or 0,
                    }
        conn.close()
    except Exception as e:
        log.warning("读取 users.db 失败: %s", e)

    # 3. 从 accounts.json 读取（覆盖同 student_id，补全 ticket/token）
    try:
        from sso.sso_common import load_data

        data = load_data()
        for acc in data.get("accounts", []):
            if not (acc.get("access_token") or acc.get("portal_ticket")):
                continue
            sid = (acc.get("student_id", "") or "").strip()
            if not sid:
                continue
            existing = seen.get(sid, {})
            seen[sid] = {
                "qq": existing.get("qq", 0),
                "student_id": sid,
                "realname": acc.get("realname", existing.get("realname", "")),
                "portal_ticket": acc.get(
                    "portal_ticket", existing.get("portal_ticket", "")
                ),
                "access_token": acc.get(
                    "access_token", existing.get("access_token", "")
                ),
                "expires_at": acc.get("expires_at", existing.get("expires_at", 0)),
            }
    except Exception as e:
        log.warning("读取 accounts.json 失败: %s", e)

    users = list(seen.values())
    return users


# ════════════════════ 格式化输出 ════════════════════


def _display(value: object) -> str:
    return str(value) if value not in (None, "") else "未获取"


def _score(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def format_secondclass_summary(data: dict) -> str:
    """格式化二课信息为易读文本"""
    return (
        "━━ 第二课堂信息 ━━\n"
        f"姓名: {_display(data.get('realname'))}\n"
        f"学号: {_display(data.get('student_id'))}\n"
        f"班级: {_display(data.get('deptname'))}\n"
        f"学院: {_display(data.get('college'))}\n"
        f"专业: {_display(data.get('major'))}\n"
        "━━ 活动概况 ━━\n"
        f"我的活动: {int(data.get('activity_count') or 0)} 个\n"
        f"未签到活动: {int(data.get('unsigned_count') or 0)} 个\n"
        f"未提交总结: {int(data.get('unfinished_count') or 0)} 个\n"
        f"我的社团: {int(data.get('club_count') or 0)} 个\n"
        "━━ 积分汇总 ━━\n"
        f"二课总分: {_score(data.get('total_score')):.1f} 分\n"
        f"累计时长: {_score(data.get('total_duration')):.1f} 小时\n"
        "━━ 当前学期分类 ━━\n"
        f"🇨🇳 思想政治: {_score(data.get('ideology_score')):.1f} 分\n"
        f"🛠️ 劳动教育: {_score(data.get('labor_score')):.1f} 分\n"
        f"🎨 文艺美育: {_score(data.get('art_score')):.1f} 分\n"
        f"🤝 志愿服务: {_score(data.get('volunteer_score')):.1f} 分\n"
        "━━ 全部学年总积分 ━━\n"
        f"🇨🇳 思想政治: {_score(data.get('ideology_score_all')):.1f} 分\n"
        f"🛠️ 劳动教育: {_score(data.get('labor_score_all')):.1f} 分\n"
        f"🎨 文艺美育: {_score(data.get('art_score_all')):.1f} 分\n"
        f"🤝 志愿服务: {_score(data.get('volunteer_score_all')):.1f} 分"
    )


def format_activity_summary(activities: list[dict], title: str = "可报名活动") -> str:
    """格式化活动列表为可读文本。"""
    if not activities:
        return f"━━ {title} ━━\n暂无活动数据"

    lines = [f"━━ {title}（共{len(activities)}个）━━"]
    for i, act in enumerate(activities, 1):
        lines.append(f"{i}. {act['activity_name']}")
        lines.append(f"   积分: {act['score']}  模块: {act['module_name']}")
        lines.append(f"   活动: {act['start_date']} ~ {act['end_date']}")
        if act.get("apply_start") and act.get("apply_end"):
            lines.append(f"   报名: {act['apply_start']} ~ {act['apply_end']}")
        lines.append(f"   主办: {act['organizer']}")
        if act.get("status_name"):
            lines[-1] += f"   [{act['status_name']}]"

    max_len = 500  # 文本长度限制
    result = "\n".join(lines)
    if len(result) > max_len:
        result = result[:max_len] + "\n...（余略）"
    return result


# ════════════════════ 活动报名 ════════════════════


class ActivityApplyError(RuntimeError):
    """活动报名流程异常。"""


def fetch_apply_page(sess: requests.Session, activity_id: str) -> dict:
    """获取活动报名页面，提取隐藏字段 s1/s2 并检测是否可报名。

    Args:
        sess: 已认证的 requests.Session（含 SSID）。
        activity_id: 活动 ID。

    Returns:
        dict: {
            "s1": str, "s2": str,          # 隐藏表单字段
            "activity_name": str,           # 活动名称
            "need_captcha": bool,           # 是否需要验证码
            "status_name": str,             # 当前状态（如 "已报名"、"报名中"）
        }

    Raises:
        ActivityApplyError: 页面不可访问、已报名、或不在报名期。
        SecondClassAuthError: 认证失效。
    """
    r = sess.get(
        ACTIVITY_DETAIL_URL,
        params={
            "activityID": activity_id,
            "retUrl": "/Student/Activity/index.html",
        },
        timeout=15,
        headers={"Referer": f"{BASE_URL}/Student/Activity/index.html"},
    )
    if r.status_code != 200:
        raise ActivityApplyError(f"活动页面返回 {r.status_code}")

    # 检查是否被重定向到登录页
    # 注意：RequestsCookieJar.get() 大小写不敏感，但服务端下发的是大写 "SSID"
    if "login" in r.url.lower() or not sess.cookies.get("SSID"):
        cookie_names = {c.name for c in sess.cookies}
        log.warning(
            "fetch_apply_page 疑似登录失效: url=%s cookies=%s status=%s",
            r.url, sorted(cookie_names), r.status_code,
        )
        raise SecondClassAuthError("二课登录已过期，请重新 #扫码登录")

    soup = BeautifulSoup(r.text, "html.parser")

    # 提取活动名称
    activity_name = ""
    name_el = soup.select_one("b.mui-pull-left, div.mui-content-padded b")
    if name_el:
        activity_name = name_el.get_text(strip=True)
    if not activity_name:
        title_tag = soup.find("title")
        if title_tag:
            activity_name = title_tag.get_text(strip=True)

    # 检测当前状态：优先从 span.mui-btn-danger > div.btn 取状态文本
    status_name = ""
    status_el = soup.select_one(
        "span.mui-btn-danger div.btn, span.btn.red, span.baom, "
        "span.mui-btn div.btn, span.mui-btn-danger"
    )
    if status_el:
        status_name = status_el.get_text(strip=True)

    page_text = soup.get_text(separator="\n", strip=True)

    # 1) 已报名：apply.html 在已报名状态下会展示 ok2.png 作为成功标识
    already_applied = bool(
        soup.select_one("img[src*='ok2.png'], img[src*='ok2']")
    ) or "你已经报名" in page_text or "已经报名" in page_text
    if already_applied:
        raise ActivityApplyError(f"你已经报名活动「{activity_name}」")

    # 2) 报名未开始
    if "报名未开始" in page_text or status_name in ("报名未开始", "未开始"):
        raise ActivityApplyError(f"活动「{activity_name}」报名未开始")

    # 3) 报名已结束 / 已截止 / 活动已结束
    if (
        "报名已结束" in page_text
        or "报名截止" in page_text
        or "报名已截止" in page_text
        or status_name in ("已结束", "已截止", "报名结束", "报名已截止", "报名已结束")
    ):
        raise ActivityApplyError(f"活动「{activity_name}」报名已截止")

    # 4) 人数已满
    if (
        "人数已满" in page_text
        or "报名已满" in page_text
        or "名额已满" in page_text
        or status_name in ("已满", "人数已满", "报名已满")
    ):
        raise ActivityApplyError(f"活动「{activity_name}」报名人数已满")

    # 提取隐藏字段 s1, s2（在 apply.html 的 <form> 中）
    s1 = ""
    s2 = ""
    for hidden in soup.select("input[type=hidden]"):
        name = hidden.get("name", "")
        val = hidden.get("value", "")
        if name == "s1":
            s1 = val
        elif name == "s2":
            s2 = val

    # 兜底：从 JavaScript 中提取 s1/s2 赋值
    # 实际页面使用对象字面量语法：s1:"29704", s2:"78df...c4fdc"
    # 也兼容 s1 = "..." / s1 = ... 的写法
    if not s1 or not s2:
        for script in soup.select("script"):
            js = script.get_text()
            if not s1:
                m = re.search(r'\bs1\s*[:=]\s*["\']([^"\']+)["\']', js) \
                    or re.search(r'\bs1\s*[:=]\s*(\w+)', js)
                if m:
                    s1 = m.group(1)
            if not s2:
                m = re.search(r'\bs2\s*[:=]\s*["\']([^"\']+)["\']', js) \
                    or re.search(r'\bs2\s*[:=]\s*(\w+)', js)
                if m:
                    s2 = m.group(1)

    if not s1 or not s2:
        log.warning("apply 页面未找到 s1/s2 隐藏字段，activity_id=%s", activity_id)

    # 判断是否需要验证码（verifycode 页面存在即需要）
    need_captcha = bool(
        soup.select_one("img[src*='verifycode'], input[name=activityApplyRand]")
    )
    if not need_captcha:
        # 兜底：页面文本含"验证码"
        need_captcha = "验证码" in page_text

    log.info(
        "apply 页面解析: id=%s name=%s s1=%s s2=%s captcha=%s status=%s",
        activity_id,
        activity_name,
        s1[:8] if s1 else "",
        s2[:8] if s2 else "",
        need_captcha,
        status_name,
    )

    return {
        "s1": s1,
        "s2": s2,
        "activity_name": activity_name,
        "need_captcha": need_captcha,
        "status_name": status_name,
    }


def fetch_verifycode_image(sess: requests.Session) -> bytes:
    """获取报名验证码图片 bytes。

    访问 verifycode.html 页面，该页面会设置 activityApplyTimes/activityApplyTimeStart
    cookies，并返回验证码图片。

    Args:
        sess: 已认证的 requests.Session（含 SSID）。

    Returns:
        验证码图片的 bytes 数据。

    Raises:
        ActivityApplyError: 获取验证码失败。
    """
    r = sess.get(
        ACTIVITY_VERIFYCODE_URL,
        timeout=15,
        headers={
            "Referer": f"{BASE_URL}/Student/Activity/index.html",
            **ANDROID_HEADERS,
        },
    )
    if r.status_code != 200:
        raise ActivityApplyError(f"验证码页面返回 {r.status_code}")

    ct = (r.headers.get("Content-Type") or "").lower()

    # 如果返回 HTML（验证码可能内嵌在页面中），尝试提取内嵌图片
    if "text/html" in ct:
        soup = BeautifulSoup(r.text, "html.parser")
        img_tag = soup.select_one("img[src*='verifycode'], img[src*='code']")
        if img_tag:
            src = img_tag.get("src", "")
            if src:
                img_url = src if src.startswith("http") else f"{BASE_URL}{src}"
                ir = sess.get(img_url, timeout=15)
                if ir.status_code == 200 and len(ir.content) > 100:
                    return ir.content

        # 尝试从 HTML 中提取 base64 图片
        for img_tag in soup.select("img[src^='data:']"):
            src = img_tag.get("src", "")
            if "base64," in src:
                import base64

                b64_data = src.split("base64,")[-1]
                img_bytes = base64.b64decode(b64_data)
                if len(img_bytes) > 100:
                    return img_bytes

        # HTML 页面无内嵌图片 → 可能验证码是纯页面渲染，直接返回完整截图不现实
        # 回退：尝试直接请求验证码图片接口
        code_url = f"{BASE_URL}/Student/Activity/verifycode.html?type=image"
        cr = sess.get(
            code_url, timeout=15, headers={"Referer": ACTIVITY_VERIFYCODE_URL}
        )
        if cr.status_code == 200 and len(cr.content) > 100:
            return cr.content

        raise ActivityApplyError("验证码页面无有效图片数据")

    # 直接返回图片二进制
    if len(r.content) < 100:
        raise ActivityApplyError(f"验证码数据异常: {len(r.content)}B")

    return r.content


def submit_activity_apply(
    sess: requests.Session,
    activity_id: str,
    captcha_code: str,
    s1: str,
    s2: str,
) -> dict:
    """提交活动报名。

    Args:
        sess: 已认证的 requests.Session（含 SSID + activityApply cookies）。
        activity_id: 活动 ID。
        captcha_code: 验证码。
        s1: 隐藏字段 s1。
        s2: 隐藏字段 s2。

    Returns:
        dict: {"success": bool, "message": str}

    Raises:
        ActivityApplyError: 提交失败。
        SecondClassAuthError: 认证失效。
    """
    data = {
        "activityID": activity_id,
        "activityApplyRand": captcha_code,
        "s1": s1,
        "s2": s2,
    }
    r = sess.post(
        ACTIVITY_APPLY_GO_URL,
        data=data,
        timeout=15,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/Student/Activity/apply.html?activityID={activity_id}"
            f"&retUrl=JTJGU3R1ZGVudCUyRkFjdGl2aXR5JTJGaW5kZXguaHRtbA==",
        },
    )
    if r.status_code != 200:
        raise ActivityApplyError(f"报名请求返回 {r.status_code}")

    # 解析响应（JSON）
    try:
        result = r.json()
    except Exception:
        # 非 JSON → 可能是认证失效返回了 HTML
        if "login" in r.text.lower() or "登录" in r.text:
            raise SecondClassAuthError("二课登录已过期，请重新 #扫码登录")
        raise ActivityApplyError(f"报名响应解析失败: {r.text[:200]}")

    success = bool(
        result.get("success")
        or result.get("result") == "1"
        or str(result.get("status")) == "1"
    )
    message = (
        result.get("message")
        or result.get("msg")
        or ("报名成功" if success else "报名失败")
    )

    log.info("活动报名结果: id=%s success=%s msg=%s", activity_id, success, message)

    return {"success": success, "message": message}


# ──────────────────────────────────────────────────────────────────────────────
# 分数编辑：服务器写入端点探针
# ──────────────────────────────────────────────────────────────────────────────

SCORE_EDIT_GET_PROBES = (
    "/Student/My/myScoreEdit.html",
    "/Student/My/myScoreItemEdit.html",
    "/Student/My/myScoreOpen.html",
    "/Student/My/scoreItem.html",
    "/Student/My/myScoreAdd.html",
    "/Student/My/scoreApply.html",
    "/Student/My/myScoreApply.html",
    "/Student/My/scoreSelf.html",
)

SCORE_EDIT_POST_PROBES = (
    "/Student/My/saveScore.html",
    "/Student/My/myScoreSave.html",
    "/Student/My/myScoreEdit_save.html",
    "/Student/My/scoreItemSave.html",
    "/Student/My/myScoreItemSave.html",
    "/Student/My/saveMyScore.html",
    "/Student/My/myScoreAdd_save.html",
    "/Student/My/scoreApply_save.html",
)


def _looks_like_edit_form(text: str) -> bool:
    """识别页面是否是真实的分数编辑表单，而不是登录跳转。"""
    if not text:
        return False
    if "top.location.href" in text:
        return False
    lower = text.lower()
    has_form = "<form" in lower or "<input" in lower
    has_score_kw = (
        ('name="score"' in lower)
        or ("分数" in text)
        or ("加分" in text)
        or ("提交" in text and "<form" in lower)
    )
    return has_form and has_score_kw


def _probe_get(sess, path: str) -> str:
    """返回命中的完整 URL；未命中返回空串。"""
    url = f"{BASE_URL}{path}"
    try:
        r = sess.get(url, timeout=8)
    except Exception as e:
        log.debug("probe GET %s 异常: %s", path, e)
        return ""
    if getattr(r, "status_code", 0) != 200:
        return ""
    if _looks_like_edit_form(getattr(r, "text", "") or ""):
        return url
    return ""


def _probe_post(sess, path: str) -> str:
    """返回命中的完整 URL；未命中返回空串。"""
    url = f"{BASE_URL}{path}"
    payload = {
        "moduleID": "2",
        "yearID": "20252026",
        "termID": "2",
        "score": "-999",
    }
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/Student/My/myScoreTotal.html?ret=",
    }
    try:
        r = sess.post(url, data=payload, headers=headers, timeout=8)
    except Exception as e:
        log.debug("probe POST %s 异常: %s", path, e)
        return ""
    if getattr(r, "status_code", 0) != 200:
        return ""
    try:
        data = r.json()
    except Exception:
        return ""
    # 返回 JSON 即说明端点存在（业务上参数错误也算可达）
    if isinstance(data, (dict, list)):
        log.info("probe POST 命中: %s -> %s", path, str(data)[:120])
        return url
    return ""


def probe_score_edit_endpoints(sess) -> dict:
    """探测二课服务器是否暴露分数编辑接口。

    返回 {
        "online_available": bool,
        "edit_page":      命中的完整 URL 或 "",
        "save_endpoint":  命中的完整 URL 或 "",
        "tried_get":      [...],
        "tried_post":     [...],
    }
    """
    edit_page = ""
    save_endpoint = ""

    for path in SCORE_EDIT_GET_PROBES:
        edit_page = _probe_get(sess, path)
        if edit_page:
            log.info("probe GET 命中: %s", edit_page)
            break

    for path in SCORE_EDIT_POST_PROBES:
        save_endpoint = _probe_post(sess, path)
        if save_endpoint:
            break

    online_available = bool(edit_page or save_endpoint)
    return {
        "online_available": online_available,
        "edit_page": edit_page,
        "save_endpoint": save_endpoint,
        "tried_get": list(SCORE_EDIT_GET_PROBES),
        "tried_post": list(SCORE_EDIT_POST_PROBES),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 分数编辑：拉明细 + 本地写入
# ──────────────────────────────────────────────────────────────────────────────

# 关键字 → 本地 second_class_v2 字段映射
# 顺序敏感：先匹配更具体的（如"职业素养"优先于"讲座"）
_SCORE_FIELD_KEYWORDS = (
    (("职业", "素养", "实习", "就业"), "career_score"),
    (("劳动", "劳动教育"), "labor_score"),
    (("文艺", "美育", "艺术"), "art_score"),
    (("志愿", "公益"), "volunteer_score"),
    (("专业", "技能", "竞赛", "学术"), "skill_score"),
    (("思想", "政治", "讲座", "党", "时政"), "thought_score"),
)


def _classify_item_to_field(item: dict) -> str:
    """根据明细项名称返回 second_class_v2 中要写入的字段名；未匹配返回 ""。"""
    name = str(item.get("item_name") or item.get("name") or "")
    if not name:
        return ""
    for keywords, field in _SCORE_FIELD_KEYWORDS:
        for kw in keywords:
            if kw in name:
                return field
    return ""


def _normalize_score_item(raw: dict, source_module_id: str) -> dict:
    """把服务器返回的原始明细项规范化。"""
    category_map = {
        "2": "思想成长与引领",
        "4": "职业精神与素质养成",
    }
    name = str(raw.get("name") or raw.get("itemName") or "")
    try:
        score = float(raw.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        limit = float(raw.get("limit") or raw.get("max") or 0)
    except (TypeError, ValueError):
        limit = 0.0
    item_id = str(raw.get("id") or raw.get("itemID") or raw.get("scoreID") or "")
    return {
        "category": category_map.get(source_module_id, "其他"),
        "item_name": name,
        "score": score,
        "limit": limit,
        "item_id": item_id,
        "source_module_id": source_module_id,
    }


def fetch_score_items_with_session(
    sess, year_id: str = "20252026", term_id: str = "2"
) -> list[dict]:
    """合并 moduleID=2(思想政治) + moduleID=4(实践美育) 的明细项。

    返回 [{category, item_name, score, limit, item_id, source_module_id}, ...]
    """
    items: list[dict] = []
    for module_id in ("2", "4"):
        try:
            data = fetch_module_scores_with_session(
                sess, module_id=module_id, year_id=year_id, term_id=term_id
            )
        except Exception as e:
            log.error("fetch_score_items: module=%s 失败: %s", module_id, e)
            continue
        raw_list = data.get("raw", []) if isinstance(data, dict) else []
        if not isinstance(raw_list, list):
            log.warning("module=%s raw 非 list: %r", module_id, type(raw_list))
            continue
        for raw_item in raw_list:
            if isinstance(raw_item, dict):
                items.append(_normalize_score_item(raw_item, module_id))

    log.info("fetch_score_items: 共 %d 项 (year=%s, term=%s)", len(items), year_id, term_id)
    return items


def save_score_item_local(
    student_id: str,
    item: dict,
    new_score: float,
    db: "SecondClassDB | None" = None,
) -> dict:
    """本地写入：把 item 对应字段更新为 new_score，其他字段保留。

    返回 {success, mode, field, old_score, new_score, message}
    """
    if not student_id:
        return {
            "success": False,
            "mode": "local",
            "field": "",
            "old_score": 0.0,
            "new_score": new_score,
            "message": "student_id 为空",
        }

    field = _classify_item_to_field(item)
    if not field:
        return {
            "success": False,
            "mode": "local",
            "field": "",
            "old_score": 0.0,
            "new_score": new_score,
            "message": f"无法分类: {item.get('item_name', '')}",
        }

    if db is None:
        db = SecondClassDB()

    # 先取整行；不存在则用空字典 merge
    existing = db.get_by_student_id(student_id) or {}
    old_score = float(existing.get(field) or 0)

    # 保留所有字段，仅覆盖目标字段
    merged_kw: dict = {}
    preservable = [
        "realname",
        "deptname",
        "college",
        "major",
        "year_id",
        "semester_info",
        "total_score",
        "activity_count",
        "unsigned_count",
        "unfinished_count",
        "club_count",
        "thought_score",
        "skill_score",
        "career_score",
        "total_score_all",
        "thought_score_all",
        "skill_score_all",
        "career_score_all",
        "ideology_score",
        "labor_score",
        "art_score",
        "volunteer_score",
        "ideology_score_all",
        "labor_score_all",
        "art_score_all",
        "volunteer_score_all",
        "total_duration",
        "total_duration_all",
        "ideology_duration",
        "labor_duration",
        "art_duration",
        "volunteer_duration",
        "ideology_duration_all",
        "labor_duration_all",
        "art_duration_all",
        "volunteer_duration_all",
    ]
    for f in preservable:
        if f in existing:
            merged_kw[f] = existing[f]
    merged_kw[field] = float(new_score)

    qq = int(existing.get("qq") or 0)
    db.upsert(student_id=student_id, qq=qq, **merged_kw)

    log.info(
        "save_score_item_local: student_id=%s field=%s %.2f → %.2f",
        student_id,
        field,
        old_score,
        new_score,
    )
    return {
        "success": True,
        "mode": "local",
        "field": field,
        "old_score": old_score,
        "new_score": float(new_score),
        "message": f"已写本地: {field}={new_score}",
    }




# ──────────────────────────────────────────────────────────────────────────────
# 分数编辑：拉明细 + 本地写入
# ──────────────────────────────────────────────────────────────────────────────

# 关键字 → 本地 second_class_v2 字段映射
# 顺序敏感：先匹配更具体的（如"职业素养"优先于"讲座"）
_SCORE_FIELD_KEYWORDS = (
    (("职业", "素养", "实习", "就业"), "career_score"),
    (("劳动", "劳动教育"), "labor_score"),
    (("文艺", "美育", "艺术"), "art_score"),
    (("志愿", "公益"), "volunteer_score"),
    (("专业", "技能", "竞赛", "学术"), "skill_score"),
    (("思想", "政治", "讲座", "党", "时政"), "thought_score"),
)


def _classify_item_to_field(item: dict) -> str:
    """根据明细项名称返回 second_class_v2 中要写入的字段名；未匹配返回 ""。"""
    name = str(item.get("item_name") or item.get("name") or "")
    if not name:
        return ""
    for keywords, field in _SCORE_FIELD_KEYWORDS:
        for kw in keywords:
            if kw in name:
                return field
    return ""


def _normalize_score_item(raw: dict, source_module_id: str) -> dict:
    """把服务器返回的原始明细项规范化。"""
    category_map = {
        "2": "思想成长与引领",
        "4": "职业精神与素质养成",
    }
    name = str(raw.get("name") or raw.get("itemName") or "")
    try:
        score = float(raw.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        limit = float(raw.get("limit") or raw.get("max") or 0)
    except (TypeError, ValueError):
        limit = 0.0
    item_id = str(raw.get("id") or raw.get("itemID") or raw.get("scoreID") or "")
    return {
        "category": category_map.get(source_module_id, "其他"),
        "item_name": name,
        "score": score,
        "limit": limit,
        "item_id": item_id,
        "source_module_id": source_module_id,
    }


def fetch_score_items_with_session(
    sess, year_id: str = "20252026", term_id: str = "2"
) -> list[dict]:
    """合并 moduleID=2(思想政治) + moduleID=4(实践美育) 的明细项。

    返回 [{category, item_name, score, limit, item_id, source_module_id}, ...]
    """
    items: list[dict] = []
    for module_id in ("2", "4"):
        try:
            data = fetch_module_scores_with_session(
                sess, module_id=module_id, year_id=year_id, term_id=term_id
            )
        except Exception as e:
            log.error("fetch_score_items: module=%s 失败: %s", module_id, e)
            continue
        raw_list = data.get("raw", []) if isinstance(data, dict) else []
        if not isinstance(raw_list, list):
            log.warning("module=%s raw 非 list: %r", module_id, type(raw_list))
            continue
        for raw_item in raw_list:
            if isinstance(raw_item, dict):
                items.append(_normalize_score_item(raw_item, module_id))

    log.info("fetch_score_items: 共 %d 项 (year=%s, term=%s)", len(items), year_id, term_id)
    return items


def save_score_item_local(
    student_id: str,
    item: dict,
    new_score: float,
    db: "SecondClassDB | None" = None,
) -> dict:
    """本地写入：把 item 对应字段更新为 new_score，其他字段保留。

    返回 {success, mode, field, old_score, new_score, message}
    """
    if not student_id:
        return {
            "success": False,
            "mode": "local",
            "field": "",
            "old_score": 0.0,
            "new_score": new_score,
            "message": "student_id 为空",
        }

    field = _classify_item_to_field(item)
    if not field:
        return {
            "success": False,
            "mode": "local",
            "field": "",
            "old_score": 0.0,
            "new_score": new_score,
            "message": f"无法分类: {item.get('item_name', '')}",
        }

    if db is None:
        db = SecondClassDB()

    # 先取整行；不存在则用空字典 merge
    existing = db.get_by_student_id(student_id) or {}
    old_score = float(existing.get(field) or 0)

    # 保留所有字段，仅覆盖目标字段
    merged_kw: dict = {}
    preservable = [
        "realname",
        "deptname",
        "college",
        "major",
        "year_id",
        "semester_info",
        "total_score",
        "activity_count",
        "unsigned_count",
        "unfinished_count",
        "club_count",
        "thought_score",
        "skill_score",
        "career_score",
        "total_score_all",
        "thought_score_all",
        "skill_score_all",
        "career_score_all",
        "ideology_score",
        "labor_score",
        "art_score",
        "volunteer_score",
        "ideology_score_all",
        "labor_score_all",
        "art_score_all",
        "volunteer_score_all",
        "total_duration",
        "total_duration_all",
        "ideology_duration",
        "labor_duration",
        "art_duration",
        "volunteer_duration",
        "ideology_duration_all",
        "labor_duration_all",
        "art_duration_all",
        "volunteer_duration_all",
    ]
    for f in preservable:
        if f in existing:
            merged_kw[f] = existing[f]
    merged_kw[field] = float(new_score)

    qq = int(existing.get("qq") or 0)
    db.upsert(student_id=student_id, qq=qq, **merged_kw)

    log.info(
        "save_score_item_local: student_id=%s field=%s %.2f → %.2f",
        student_id,
        field,
        old_score,
        new_score,
    )
    return {
        "success": True,
        "mode": "local",
        "field": field,
        "old_score": old_score,
        "new_score": float(new_score),
        "message": f"已写本地: {field}={new_score}",
    }
