"""预约报名的 DB 层。

复用 second_class_users 表（由 SecondClassUserActivityDB 创建/维护），
但本模块只读写其中的 reserved_activity_ids / reservations_meta 两列，
和 unfinished 部分解耦。表 schema 见 secondclass.secondclass_tool。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


class ReservationDB:
    """预约报名存储（second_class_users 的 reserved_* 视图）。"""

    TABLE = "second_class_users"

    def __init__(self, db_path: Path | None = None) -> None:
        # 默认走 secondclass 那张库，避免双源
        if db_path is None:
            from secondclass.secondclass_tool import USER_DB_FILE
            db_path = USER_DB_FILE
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._ensure_table()

    # ── 内部 ──

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_table(self) -> None:
        """若 second_class_users 表尚未创建（极端情况下早于 SecondClassUserActivityDB 调用），
        触发一次它的 _init_db 以保证表存在。"""
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (self.TABLE,),
            ).fetchone()
            conn.close()
        if not row:
            from secondclass.secondclass_tool import SecondClassUserActivityDB
            SecondClassUserActivityDB(self._path)

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

    def _get_row(self, conn: sqlite3.Connection, *, qq: int = 0, student_id: str = "") -> sqlite3.Row | None:
        if student_id:
            return conn.execute(
                f"SELECT * FROM {self.TABLE} WHERE student_id=?", (student_id,)
            ).fetchone()
        if qq:
            return conn.execute(
                f"SELECT * FROM {self.TABLE} WHERE qq=?", (qq,)
            ).fetchone()
        return None

    # ── 写 ──

    def add(self, *, qq: int, student_id: str, activity_id: str, meta: dict) -> bool:
        """新增一条预约；同 activity_id 已存在返回 False。"""
        if not (qq and student_id and activity_id):
            return False
        now_iso = datetime.now().isoformat(timespec="seconds")
        meta = dict(meta or {})
        meta.setdefault("notified", [])
        meta["added_at"] = now_iso
        with self._lock:
            conn = self._connect()
            row = self._get_row(conn, student_id=student_id) or self._get_row(conn, qq=qq)
            if row:
                resv = self._split(row["reserved_activity_ids"])
                if activity_id in resv:
                    conn.close()
                    return False
                resv.append(activity_id)
                try:
                    meta_all = json.loads(row["reservations_meta"] or "{}")
                except Exception:
                    meta_all = {}
                meta_all[activity_id] = meta
                conn.execute(
                    f"""UPDATE {self.TABLE}
                        SET reserved_activity_ids=?, reservations_meta=?,
                            qq=?, student_id=?, updated_at=?
                        WHERE qq=?""",
                    (self._join(resv), json.dumps(meta_all, ensure_ascii=False),
                     qq, student_id, now_iso, row["qq"]),
                )
            else:
                conn.execute(
                    f"""INSERT INTO {self.TABLE}
                        (qq, student_id, reserved_activity_ids, reservations_meta, updated_at)
                        VALUES (?, ?, ?, ?, ?)""",
                    (qq, student_id, activity_id,
                     json.dumps({activity_id: meta}, ensure_ascii=False), now_iso),
                )
            conn.commit()
            conn.close()
        return True

    def remove(self, *, activity_id: str, qq: int = 0, student_id: str = "") -> bool:
        if not activity_id or not (qq or student_id):
            return False
        now_iso = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            row = self._get_row(conn, student_id=student_id, qq=qq)
            if not row:
                conn.close()
                return False
            resv = self._split(row["reserved_activity_ids"])
            if activity_id not in resv:
                conn.close()
                return False
            try:
                meta_all = json.loads(row["reservations_meta"] or "{}")
            except Exception:
                meta_all = {}
            meta_all.pop(activity_id, None)
            conn.execute(
                f"""UPDATE {self.TABLE}
                    SET reserved_activity_ids=?, reservations_meta=?, updated_at=?
                    WHERE qq=?""",
                (self._join([x for x in resv if x != activity_id]),
                 json.dumps(meta_all, ensure_ascii=False), now_iso, row["qq"]),
            )
            conn.commit()
            conn.close()
        return True

    def patch_meta(self, *, qq: int, activity_id: str, patch: dict) -> bool:
        if not (qq and activity_id and patch):
            return False
        with self._lock:
            conn = self._connect()
            row = self._get_row(conn, qq=qq)
            if not row:
                conn.close()
                return False
            try:
                meta_all = json.loads(row["reservations_meta"] or "{}")
            except Exception:
                meta_all = {}
            cur = meta_all.get(activity_id, {})
            cur.update(patch)
            meta_all[activity_id] = cur
            conn.execute(
                f"UPDATE {self.TABLE} SET reservations_meta=? WHERE qq=?",
                (json.dumps(meta_all, ensure_ascii=False), qq),
            )
            conn.commit()
            conn.close()
        return True

    # ── 读 ──

    def get_user(self, *, qq: int = 0, student_id: str = "") -> dict:
        """返回 {activity_id: meta_dict}（单用户）。"""
        with self._lock:
            conn = self._connect()
            row = self._get_row(conn, qq=qq, student_id=student_id)
            conn.close()
        if not row:
            return {}
        try:
            return json.loads(row["reservations_meta"] or "{}")
        except Exception:
            return {}

    def get_all(self) -> list[dict]:
        """全部预约：[{qq, student_id, activity_id, meta}]."""
        out: list[dict] = []
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                f"SELECT qq, student_id, reserved_activity_ids, reservations_meta "
                f"FROM {self.TABLE} WHERE reserved_activity_ids != ''"
            ).fetchall()
            conn.close()
        for r in rows:
            try:
                meta_all = json.loads(r["reservations_meta"] or "{}")
            except Exception:
                meta_all = {}
            for aid in self._split(r["reserved_activity_ids"]):
                out.append({
                    "qq": r["qq"],
                    "student_id": r["student_id"],
                    "activity_id": aid,
                    "meta": meta_all.get(aid, {}),
                })
        return out
