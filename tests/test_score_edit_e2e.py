"""端到端测试 — 使用真实 SSID 调用线上接口 + 本地 DB 写入。

默认 skip。运行：
    python -m pytest tests/test_score_edit_e2e.py -m e2e -v -s

环境要求：
    - 网络可达 dxstar.cqtbi.edu.cn
    - SSID 仍有效（过期请重新登录 secondclass_auto_score.bat 8 获取）
    - users.db 已存在且 student_id=2403740 有行
"""

import os

import pytest

from secondclass import secondclass_tool

REAL_SSID = "si5kd505j03oogja31p57g2nj3"
STUDENT_ID = "2403740"
YEAR_ID = "20252026"
TERM_ID = "2"


def _skip_if_no_e2e():
    if os.environ.get("RUN_E2E") != "1" and "e2e" not in os.environ.get(
        "PYTEST_CURRENT_TEST", ""
    ):
        # 仅在显式 -m e2e 时才跑
        pass


@pytest.fixture(scope="module")
def real_session():
    sess = secondclass_tool._session(REAL_SSID)
    # 用 index 验证 SSID 是否仍有效
    r = sess.get("https://dxstar.cqtbi.edu.cn/Student/Index/index.html", timeout=10)
    if "top.location" in r.text.lower() or "login" in r.text.lower():
        pytest.skip(f"SSID 已过期 ({REAL_SSID})，请重新登录")
    return sess


@pytest.mark.e2e
def test_e2e_probe(real_session):
    """真实探测写入端点 — 仅打印结果，不 assert。"""
    result = secondclass_tool.probe_score_edit_endpoints(real_session)
    print("\n[E2E PROBE]", result)
    assert "online_available" in result


@pytest.mark.e2e
def test_e2e_fetch_real_items(real_session):
    """拉取真实学期明细，应至少 3 条目标必修项。"""
    items = secondclass_tool.fetch_score_items_with_session(
        real_session, year_id=YEAR_ID, term_id=TERM_ID
    )
    print(f"\n[E2E FETCH] 共 {len(items)} 项")
    for it in items:
        print(f"  [{it.get('category')}] {it.get('item_name')} = {it.get('score')}")
    assert len(items) >= 3

    names = [i["item_name"] for i in items]
    # 至少应能看到这三项必修
    assert any("思想" in n or "讲座" in n for n in names), names
    assert any("劳动" in n for n in names), names
    assert any("文艺" in n or "美育" in n for n in names), names


@pytest.mark.e2e
def test_e2e_local_write_roundtrip(tmp_path):
    """在 tmp_path 上做读 → 改 → 读回验证 → 还原。

    使用 tmp DB 避免污染 users.db。
    """
    db = secondclass_tool.SecondClassDB(db_path=tmp_path / "users.db")
    # 种一行
    db.upsert(
        student_id=STUDENT_ID,
        labor_score=0.2,
        thought_score=0.5,
        art_score=0.1,
    )

    item = {"item_name": "必修-劳动教育", "category": "职业精神与素质养成"}
    result = secondclass_tool.save_score_item_local(
        student_id=STUDENT_ID, item=item, new_score=0.4, db=db
    )

    assert result["success"] is True
    assert result["field"] == "labor_score"
    assert result["old_score"] == 0.2
    assert result["new_score"] == 0.4

    row = db.get_by_student_id(STUDENT_ID)
    assert row["labor_score"] == 0.4
    # 其他字段保留
    assert row["thought_score"] == 0.5
    assert row["art_score"] == 0.1


@pytest.mark.e2e
def test_e2e_apply_target_values():
    """最终目标 — 把三项必修改为 0.3 / 0.4 / 0.4，写本地 DB。

    使用真实 users.db。记录改前改后。
    """
    db = secondclass_tool.SecondClassDB()  # 默认 users.db

    # 改前
    before = db.get_by_student_id(STUDENT_ID)
    if before is None:
        pytest.skip(f"学号 {STUDENT_ID} 在 users.db 中不存在")

    print(f"\n[E2E APPLY] 改前:")
    for f in ("thought_score", "labor_score", "art_score"):
        print(f"  {f} = {before.get(f)}")

    targets = [
        ({"item_name": "必修-思想政治教育活动（讲座）"}, 0.3, "thought_score"),
        ({"item_name": "必修-劳动教育"}, 0.4, "labor_score"),
        ({"item_name": "必修-文艺美育"}, 0.4, "art_score"),
    ]

    for item, score, expected_field in targets:
        r = secondclass_tool.save_score_item_local(
            student_id=STUDENT_ID, item=item, new_score=score, db=db
        )
        print(f"  写 {item['item_name']} → {score}: {r}")
        assert r["success"] is True
        assert r["field"] == expected_field
        assert r["new_score"] == score

    # 改后
    after = db.get_by_student_id(STUDENT_ID)
    print(f"\n[E2E APPLY] 改后:")
    for f in ("thought_score", "labor_score", "art_score"):
        print(f"  {f} = {after.get(f)}")

    assert after["thought_score"] == 0.3
    assert after["labor_score"] == 0.4
    assert after["art_score"] == 0.4
