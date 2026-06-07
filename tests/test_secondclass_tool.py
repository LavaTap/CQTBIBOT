import sqlite3
import time

import pytest

from secondclass import secondclass_tool


class FakeResponse:
    def __init__(self, text="", json_data=None, status_code=200):
        self.text = text
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")

    def json(self):
        if self._json_data is not None:
            return self._json_data
        raise ValueError("No JSON data")


class FakeSession:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []
        self.cookies = {}
        self.headers = {}
        self.trust_env = True
        self.next_get = FakeResponse()
        self.next_post = FakeResponse(json_data=[])

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.next_get

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.next_post


def test_secondclass_db_creates_requested_columns(tmp_path):
    db_path = tmp_path / "users.db"

    secondclass_tool.SecondClassDB(db_path=db_path)

    conn = sqlite3.connect(db_path)
    # 检查 v2 表 schema
    columns_v2 = {row[1] for row in conn.execute("PRAGMA table_info(second_class_v2)")}
    conn.close()

    assert {
        "student_id",
        "qq",
        "realname",
        "deptname",
        "college",
        "major",
        "year_id",
        "total_score",
        "activity_count",
        "unsigned_count",
        "unfinished_count",
        "club_count",
        "thought_score",
        "skill_score",
        "career_score",
        "updated_at",
    } <= columns_v2


def test_secondclass_db_upsert_persists_user_linkage_and_scores(tmp_path):
    db = secondclass_tool.SecondClassDB(db_path=tmp_path / "users.db")

    db.upsert(
        student_id="2400001",
        qq=123,
        realname="张三",
        deptname="软件2401",
        college="智能制造学院",
        major="软件技术",
        year_id="20252026",
        total_score=12.5,
        activity_count=4,
        unsigned_count=1,
        unfinished_count=2,
        club_count=3,
        thought_score=1.0,
        skill_score=2.0,
        career_score=3.0,
    )

    row = db.get_by_student_id("2400001")

    assert row["student_id"] == "2400001"
    assert row["qq"] == 123
    assert row["realname"] == "张三"
    assert row["deptname"] == "软件2401"
    assert row["college"] == "智能制造学院"
    assert row["major"] == "软件技术"
    assert row["year_id"] == "20252026"
    assert row["total_score"] == 12.5
    assert row["activity_count"] == 4
    assert row["unsigned_count"] == 1
    assert row["unfinished_count"] == 2
    assert row["club_count"] == 3
    assert row["thought_score"] == 1.0
    assert row["skill_score"] == 2.0
    assert row["career_score"] == 3.0

    # 也可通过 qq 查询
    row_by_qq = db.get_by_qq(123)
    assert row_by_qq is not None
    assert row_by_qq["student_id"] == "2400001"


def test_fetch_index_counts_with_session_parses_dashboard_counts():
    sess = FakeSession()
    sess.next_get = FakeResponse(
        text="""
        <html><body>
        <ul class="my_list">
            <li class="my1"><a href="/Student/My/myActivity.html"><span>7</span>我的活动</a></li>
            <li class="my10"><a href="/Student/My/activityNoSign.html"><span>2</span>未签到活动</a></li>
            <li class="my11"><a href="/Student/My/activityNoSummary.html"><span>3</span>未提交总结的活动</a></li>
            <li class="my4"><a href="/Student/Association/myAssociation.html"><span>5</span>我的社团</a></li>
        </ul>
        </body></html>
        """
    )

    result = secondclass_tool.fetch_index_counts_with_session(sess)

    assert result["activity_count"] == 7
    assert result["unsigned_count"] == 2
    assert result["unfinished_count"] == 3
    assert result["club_count"] == 5
    assert result.get("college") == ""
    assert result.get("major") == ""
    assert sess.get_calls[0][0].endswith("/Student/My/index.html")


def test_fetch_index_counts_with_session_parses_college_major():
    sess = FakeSession()
    sess.next_get = FakeResponse(
        text="""
        <html><body>
        <div>学院：智能制造学院</div>
        <div>专业：大数据技术</div>
        <ul class="my_list">
            <li class="my1"><a href="/Student/My/myActivity.html"><span>7</span>我的活动</a></li>
            <li class="my10"><a href="/Student/My/activityNoSign.html"><span>2</span>未签到活动</a></li>
        </ul>
        </body></html>
        """
    )

    result = secondclass_tool.fetch_index_counts_with_session(sess)

    assert result["college"] == "智能制造学院"
    assert result["major"] == "大数据技术"


def test_fetch_total_scores_with_session_sums_list_payload():
    sess = FakeSession()
    sess.next_post = FakeResponse(
        json_data=[
            {"name": "思想成长", "score": "1.5"},
            {"name": "专业技能", "score": 2},
        ]
    )

    result = secondclass_tool.fetch_total_scores_with_session(sess)

    assert result["total_score"] == 3.5


def test_fetch_semester_scores_with_session_posts_current_year_and_parses_categories():
    sess = FakeSession()
    sess.next_post = FakeResponse(
        json_data=[
            {"name": "思想成长", "score": "1.0"},
            {"name": "专业技能", "score": "2.0"},
            {"name": "职业技能", "score": "3.0"},
        ]
    )

    result = secondclass_tool.fetch_semester_scores_with_session(
        sess, year_id="20252026"
    )

    assert result == {"thought_score": 1.0, "skill_score": 2.0, "career_score": 3.0}
    assert sess.post_calls[0][1]["data"] == {"yearID": "20252026", "termID": ""}


def test_fetch_all_with_session_preserves_identity_fields():
    sess = FakeSession()
    sess.next_get = FakeResponse(
        text="""
        <html><body>
        <ul class="my_list">
            <li class="my1"><a href="/Student/My/myActivity.html"><span>1</span>我的活动</a></li>
            <li class="my10"><a href="/Student/My/activityNoSign.html"><span>0</span>未签到活动</a></li>
            <li class="my11"><a href="/Student/My/activityNoSummary.html"><span>2</span>未提交总结的活动</a></li>
            <li class="my4"><a href="/Student/Association/myAssociation.html"><span>3</span>我的社团</a></li>
        </ul>
        </body></html>
        """
    )
    sess.next_post = FakeResponse(json_data=[{"name": "思想成长", "score": 4}])

    result = secondclass_tool.fetch_all_with_session(
        sess,
        student_id="2400001",
        realname="张三",
        deptname="软件2401",
        college="智能制造学院",
        major="软件技术",
        year_id="20252026",
    )

    assert result["student_id"] == "2400001"
    assert result["realname"] == "张三"
    assert result["deptname"] == "软件2401"
    assert result["college"] == "智能制造学院"
    assert result["major"] == "软件技术"


def test_format_secondclass_summary_contains_identity_counts_and_scores():
    text = secondclass_tool.format_secondclass_summary(
        {
            "student_id": "2400001",
            "realname": "张三",
            "deptname": "软件2401",
            "college": "智能制造学院",
            "major": "软件技术",
            "activity_count": 4,
            "unsigned_count": 1,
            "unfinished_count": 2,
            "club_count": 3,
            "total_score": 12.5,
            "thought_score": 1,
            "skill_score": 2,
            "career_score": 3,
        }
    )

    for part in [
        "姓名: 张三",
        "学号: 2400001",
        "班级: 软件2401",
        "学院: 智能制造学院",
        "专业: 软件技术",
    ]:
        assert part in text
    for part in [
        "我的活动: 4 个",
        "未签到活动: 1 个",
        "未提交总结: 2 个",
        "我的社团: 3 个",
    ]:
        assert part in text
    for part in [
        "二课总分: 12.5 分",
        "思想成长: 1.0 分",
        "专业技能: 2.0 分",
        "职业技能: 3.0 分",
    ]:
        assert part in text


def test_format_secondclass_summary_handles_missing_identity_fields():
    text = secondclass_tool.format_secondclass_summary({})

    assert "姓名: 未获取" in text
    assert "学院: 未获取" in text
    assert "专业: 未获取" in text


def test_obtain_secondclass_session_rejects_missing_credentials():
    with pytest.raises(secondclass_tool.SecondClassAuthError):
        secondclass_tool.obtain_secondclass_session_from_user({})


def test_obtain_secondclass_session_uses_cqtbi_sso_endpoint(monkeypatch):
    class CookieJar(dict):
        def get(self, key, default=None):
            return super().get(key, default)

        def set(self, key, value):
            self[key] = value

    class BridgeSession:
        def __init__(self):
            self.trust_env = True
            self.headers = {}
            self.cookies = CookieJar()
            self.get_calls = []
            self.post_calls = []

        def get(self, url, **kwargs):
            self.get_calls.append((url, kwargs))
            self.cookies["SSID"] = "fake-ssid"
            return FakeResponse(text="ok")

        def post(self, url, **kwargs):
            self.post_calls.append((url, kwargs))
            return FakeResponse(json_data=[])

        def mount(self, prefix, adapter):
            pass

    sessions = []

    def fake_session_factory():
        sess = BridgeSession()
        sessions.append(sess)
        return sess

    monkeypatch.setattr(secondclass_tool.requests, "Session", fake_session_factory)

    sess = secondclass_tool.obtain_secondclass_session_from_user(
        {"portal_ticket": "fake-ticket", "expires_at": int(time.time()) + 60}
    )

    assert sess.cookies.get("SSID") == "fake-ssid"
    assert sessions[0].trust_env is False
    # 第一步：post 到门户 dtLog!log.action
    assert any(
        u == "http://szxy.cqtbi.edu.cn/cqdddt/dtLog!log.action"
        for u, _ in sessions[0].post_calls
    )
    # 第二步：get 到二课 cqtbiSSO
    assert any(
        u == "https://2class.cqtbi.edu.cn/Admin/Index/cqtbiSSO"
        for u, _ in sessions[0].get_calls
    )
    assert any(
        p["params"] == {"PORTAL_TICKET": "fake-ticket"}
        for _, p in sessions[0].get_calls
    )


def test_get_current_year_id_returns_expected_format():
    year_id = secondclass_tool.get_current_year_id()
    assert len(year_id) == 8
    assert year_id.isdigit()
    # 形如 20252026
    assert int(year_id[:4]) >= 2024
    assert int(year_id[4:]) == int(year_id[:4]) + 1


def test_safe_json_response_parses_valid_json():
    resp = FakeResponse(json_data={"key": "value"})
    assert secondclass_tool._safe_json_response(resp) == {"key": "value"}


def test_safe_json_response_raises_auth_error_on_bad_json():
    resp = FakeResponse(text="not json")
    with pytest.raises(secondclass_tool.SecondClassAuthError):
        secondclass_tool._safe_json_response(resp, "测试")


def test_obtain_portal_ticket_returns_empty_for_no_token():
    assert secondclass_tool.obtain_portal_ticket("") == ""


def test_convert_to_ssid_with_token_only_refreshes_ticket_and_returns_ssid(monkeypatch):
    class CookieJar(dict):
        def get(self, key, default=None):
            return super().get(key, default)

        def set(self, key, value):
            self[key] = value

    class BridgeSession:
        def __init__(self):
            self.trust_env = True
            self.headers = {}
            self.cookies = CookieJar()
            self.get_calls = []
            self.post_calls = []

        def get(self, url, **kwargs):
            self.get_calls.append((url, kwargs))
            self.cookies["SSID"] = "token-ssid"
            return FakeResponse(text="ok")

        def post(self, url, **kwargs):
            self.post_calls.append((url, kwargs))
            return FakeResponse(json_data=[])

        def mount(self, prefix, adapter):
            pass

    sessions = []

    def fake_session_factory():
        sess = BridgeSession()
        sessions.append(sess)
        return sess

    monkeypatch.setattr(
        secondclass_tool, "_obtain_portal_ticket", lambda token: "fresh-ticket"
    )
    monkeypatch.setattr(secondclass_tool.requests, "Session", fake_session_factory)

    ssid = secondclass_tool.convert_to_ssid(
        access_token="token-secret", student_id="2400001"
    )

    assert ssid == "token-ssid"
    assert sessions[0].trust_env is False
    assert any(
        u == "http://szxy.cqtbi.edu.cn/cqdddt/dtLog!log.action"
        for u, _ in sessions[0].post_calls
    )
    assert any(
        u == "https://2class.cqtbi.edu.cn/Admin/Index/cqtbiSSO"
        for u, _ in sessions[0].get_calls
    )
    assert any(
        p["params"] == {"PORTAL_TICKET": "fresh-ticket"}
        for _, p in sessions[0].get_calls
    )


# ── 活动列表测试 ──


def test_parse_activity_entry_extracts_all_fields():
    raw = {
        "activityID": "121415",
        "activityName": "活动中心凳子归还",
        "applyStartDate": "1780406640",
        "applyEndDate": "1780468259",
        "startDate": "1780469400",
        "endDate": "1780471859",
        "checkAfterApply": "0",
        "organizerType": "2",
        "organizerID": "100121",
        "organizerName": "后勤管理处",
        "limitScope": "0",
        "score": "0.2",
        "img": "178040669582675.jpg",
        "status": "3",
        "moduleName": "职业精神与素质养成",
        "isClosed": "0",
        "typeID": "324",
        "moduleID": "4",
        "status2": 2,
        "status2Name": "报名中",
    }
    entry = secondclass_tool._parse_activity_entry(raw)

    assert entry["activity_id"] == "121415"
    assert entry["activity_name"] == "活动中心凳子归还"
    assert entry["score"] == 0.2
    assert entry["module_name"] == "职业精神与素质养成"
    assert entry["organizer"] == "后勤管理处"
    assert entry["status_code"] == "3"
    assert entry["status_name"] == "报名中"
    assert entry["apply_start"] != ""
    assert entry["apply_end"] != ""


def test_parse_activity_list_response_with_array():
    raw = [
        {"activityID": "1", "activityName": "活动A"},
        {"activityID": "2", "activityName": "活动B"},
    ]
    result = secondclass_tool._parse_activity_list_response(raw)
    assert len(result) == 2
    assert result[0]["activity_id"] == "1"


def test_parse_activity_list_response_with_tupled_array():
    raw = ["999", [{"activityID": "1", "activityName": "活动A"}]]
    result = secondclass_tool._parse_activity_list_response(raw)
    assert len(result) == 1
    assert result[0]["activity_id"] == "1"


def test_parse_activity_list_response_with_rows_dict():
    raw = {"rows": [{"activityID": "1", "activityName": "活动A"}]}
    result = secondclass_tool._parse_activity_list_response(raw)
    assert len(result) == 1


def test_format_activity_summary_shows_activities():
    activities = [
        {
            "activity_id": "1",
            "activity_name": "活动A",
            "score": 0.1,
            "module_name": "思想成长",
            "organizer": "某学院",
            "start_date": "06-02 19:00",
            "end_date": "06-02 22:00",
            "apply_start": "06-01 00:00",
            "apply_end": "06-02 18:00",
            "status_name": "报名中",
            "status_code": "3",
        },
    ]
    text = secondclass_tool.format_activity_summary(activities)
    assert "活动A" in text
    assert "0.1" in text
    assert "报名中" in text
    assert "06-02 19:00" in text
    assert "报名:" in text


def test_format_activity_summary_empty():
    text = secondclass_tool.format_activity_summary([])
    assert "暂无活动" in text


def test_activity_status_map():
    assert secondclass_tool.ACTIVITY_STATUS_MAP["3"] == "报名中"
    assert secondclass_tool.ACTIVITY_STATUS_MAP["5"] == "活动中"
    assert secondclass_tool.ACTIVITY_STATUS_MAP["6"] == "已结束"


def test_fetch_and_save_secondclass_info_rejects_expired(monkeypatch):
    user = {"expires_at": "0", "portal_ticket": ""}
    with pytest.raises(secondclass_tool.SecondClassAuthError):
        secondclass_tool.fetch_and_save_secondclass_info(123, user)


def test_fetch_and_save_secondclass_info_missing_credentials():
    user = {"expires_at": str(int(time.time()) + 60), "portal_ticket": ""}
    with pytest.raises(secondclass_tool.SecondClassAuthError):
        secondclass_tool.fetch_and_save_secondclass_info(456, user)


# ── v2 schema 测试 ──


def test_upsert_by_student_id_updates_on_conflict(tmp_path):
    db = secondclass_tool.SecondClassDB(db_path=tmp_path / "users.db")

    db.upsert(student_id="2400001", qq=111, realname="张三", total_score=5.0)
    db.upsert(student_id="2400001", qq=222, realname="李四", total_score=10.0)

    row = db.get_by_student_id("2400001")
    assert row["realname"] == "李四"
    assert row["total_score"] == 10.0
    # qq 更新为非零值
    assert row["qq"] == 222


def test_upsert_preserves_qq_as_reference(tmp_path):
    db = secondclass_tool.SecondClassDB(db_path=tmp_path / "users.db")

    db.upsert(student_id="2400001", qq=111, realname="张三")
    # 第二次 qq=0 不应覆盖已有的 qq
    db.upsert(student_id="2400001", qq=0, realname="张三2")

    row = db.get_by_student_id("2400001")
    assert row["qq"] == 111
    assert row["realname"] == "张三2"


def test_upsert_skips_empty_student_id(tmp_path):
    db = secondclass_tool.SecondClassDB(db_path=tmp_path / "users.db")

    # student_id 为空时应跳过，不抛异常
    db.upsert(student_id="", qq=123, realname="空学号")


def test_master_db_upsert_activities_uses_student_id(tmp_path):
    db = secondclass_tool.SecondClassMasterDB(db_path=tmp_path / "users.db")

    activities = [{"activity_id": "A001", "activity_name": "测试活动", "score": 1.0}]
    cnt = db.upsert_activities("2400001", activities, qq=123, tab="可报名")

    assert cnt == 1
    conn = sqlite3.connect(tmp_path / "users.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM second_class_master_v2 WHERE student_id='2400001'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["student_id"] == "2400001"
    assert row["qq"] == 123
    assert row["activity_id"] == "A001"


def test_master_db_get_expired_users_returns_student_ids(tmp_path):
    db = secondclass_tool.SecondClassMasterDB(db_path=tmp_path / "users.db")

    activities = [{"activity_id": "A001", "activity_name": "活动1", "score": 1.0}]
    db.upsert_activities("2400001", activities, qq=123, tab="可报名")

    result = db.get_expired_users()
    assert "2400001" in result
