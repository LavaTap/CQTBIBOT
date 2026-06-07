"""探针函数 probe_score_edit_endpoints 的单元测试。"""

from secondclass import secondclass_tool


class FakeResponse:
    def __init__(self, text="", status_code=200, json_data=None):
        self.text = text
        self.status_code = status_code
        self._json_data = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


class FakeSession:
    """按 URL 路由返回不同响应。"""

    def __init__(self, get_map=None, post_map=None, default_status=404):
        self.get_map = get_map or {}
        self.post_map = post_map or {}
        self.default_status = default_status
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        for key, resp in self.get_map.items():
            if key in url:
                return resp
        return FakeResponse(status_code=self.default_status)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        for key, resp in self.post_map.items():
            if key in url:
                return resp
        return FakeResponse(status_code=self.default_status)


def test_probe_returns_not_available_when_all_404():
    sess = FakeSession()  # 默认 404

    result = secondclass_tool.probe_score_edit_endpoints(sess)

    assert result["online_available"] is False
    assert result.get("edit_page") in (None, "")
    assert result.get("save_endpoint") in (None, "")


def test_probe_identifies_edit_page_when_form_html_returned():
    edit_html = (
        '<html><body><form><input name="score" type="text">'
        '<input type="submit" value="保存"></form></body></html>'
    )
    sess = FakeSession(
        get_map={"myScoreEdit.html": FakeResponse(text=edit_html, status_code=200)}
    )

    result = secondclass_tool.probe_score_edit_endpoints(sess)

    assert result["online_available"] is True
    assert "myScoreEdit.html" in result["edit_page"]


def test_probe_identifies_save_endpoint_when_json_returned():
    sess = FakeSession(
        post_map={
            "saveScore.html": FakeResponse(
                status_code=200, json_data={"status": 0, "msg": "参数缺失"}
            )
        }
    )

    result = secondclass_tool.probe_score_edit_endpoints(sess)

    assert result["online_available"] is True
    assert "saveScore.html" in result["save_endpoint"]


def test_probe_ignores_redirect_pages():
    """200 但内容是跳转脚本不算可用。"""
    redirect_html = (
        "<html><script>top.location.href='/Admin/Index/index.html'</script></html>"
    )
    sess = FakeSession(
        get_map={
            "myScoreEdit.html": FakeResponse(text=redirect_html, status_code=200)
        }
    )

    result = secondclass_tool.probe_score_edit_endpoints(sess)

    assert result["online_available"] is False


# ──────────────────────────────────────────────────────────────────────
# fetch_score_items_with_session + 字段分类
# ──────────────────────────────────────────────────────────────────────


def test_fetch_score_items_merges_module2_and_module4(monkeypatch):
    """合并思想政治(moduleID=2) + 实践美育(moduleID=4) 两个模块的明细项。"""
    raw_module_2 = [
        {"id": "12", "name": "必修-思想政治教育活动（讲座）", "score": "0.2", "limit": "1.0"},
    ]
    raw_module_4 = [
        {"id": "34", "name": "必修-劳动教育", "score": "0.3", "limit": "1.0"},
        {"id": "56", "name": "必修-文艺美育", "score": "0.3", "limit": "1.0"},
    ]

    def fake_module(sess, module_id, year_id, term_id):
        return {
            "raw": raw_module_2 if module_id == "2" else raw_module_4,
        }

    monkeypatch.setattr(
        secondclass_tool, "fetch_module_scores_with_session", fake_module
    )

    items = secondclass_tool.fetch_score_items_with_session(
        sess=object(), year_id="20252026", term_id="2"
    )

    assert len(items) == 3
    names = [i["item_name"] for i in items]
    assert "必修-思想政治教育活动（讲座）" in names
    assert "必修-劳动教育" in names
    assert "必修-文艺美育" in names
    # 每项字段齐全
    for it in items:
        assert "category" in it
        assert "item_name" in it
        assert "score" in it
        assert "item_id" in it
        assert "source_module_id" in it


def test_classify_item_to_field_maps_keywords():
    f = secondclass_tool._classify_item_to_field
    assert f({"item_name": "必修-思想政治教育活动（讲座）"}) == "thought_score"
    assert f({"item_name": "必修-劳动教育"}) == "labor_score"
    assert f({"item_name": "必修-文艺美育"}) == "art_score"
    assert f({"item_name": "志愿服务时长"}) == "volunteer_score"
    assert f({"item_name": "专业技能竞赛"}) == "skill_score"
    assert f({"item_name": "职业素养讲座"}) == "career_score"
    # 未知项不抛
    assert f({"item_name": "未知"}) == ""


def test_save_score_item_local_preserves_other_fields(tmp_path):
    """本地写入只改对应字段，其他字段保留。"""
    db = secondclass_tool.SecondClassDB(db_path=tmp_path / "users.db")

    # 先种一行有完整初始数据
    db.upsert(
        student_id="2403740",
        qq=12345,
        realname="测试",
        total_score=10.0,
        thought_score=1.0,
        skill_score=2.0,
        career_score=3.0,
        labor_score=0.1,
        art_score=0.1,
        volunteer_score=0.5,
    )

    item = {"item_name": "必修-劳动教育", "category": "职业精神与素质养成"}
    secondclass_tool.save_score_item_local(
        student_id="2403740", item=item, new_score=0.4, db=db
    )

    row = db.get_by_student_id("2403740")
    assert row["labor_score"] == 0.4
    # 其他字段保留
    assert row["thought_score"] == 1.0
    assert row["skill_score"] == 2.0
    assert row["career_score"] == 3.0
    assert row["art_score"] == 0.1
    assert row["volunteer_score"] == 0.5
    assert row["realname"] == "测试"
    assert row["qq"] == 12345


def test_save_score_item_local_creates_row_if_missing(tmp_path):
    """student_id 不存在时也应能写入（先建行再更新）。"""
    db = secondclass_tool.SecondClassDB(db_path=tmp_path / "users.db")

    item = {"item_name": "必修-思想政治教育活动（讲座）"}
    secondclass_tool.save_score_item_local(
        student_id="2403740", item=item, new_score=0.3, db=db
    )

    row = db.get_by_student_id("2403740")
    assert row is not None
    assert row["thought_score"] == 0.3


def test_save_score_item_local_returns_old_and_new(tmp_path):
    """返回 (old_score, new_score) 便于 audit log。"""
    db = secondclass_tool.SecondClassDB(db_path=tmp_path / "users.db")
    db.upsert(student_id="2403740", labor_score=0.2)

    result = secondclass_tool.save_score_item_local(
        student_id="2403740",
        item={"item_name": "必修-劳动教育"},
        new_score=0.4,
        db=db,
    )

    assert result["success"] is True
    assert result["mode"] == "local"
    assert result["field"] == "labor_score"
    assert result["old_score"] == 0.2
    assert result["new_score"] == 0.4

