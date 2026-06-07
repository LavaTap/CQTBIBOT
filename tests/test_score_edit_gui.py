"""分数编辑 tab 的 GUI 单元测试（FakeApp 模式，不实例化 Tk）。"""

import secondclass_auto_score_gui


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeLabel:
    def __init__(self):
        self.calls = []
        self.text = ""

    def configure(self, **kwargs):
        self.calls.append(kwargs)
        if "text" in kwargs:
            self.text = kwargs["text"]


class FakeTree:
    def __init__(self):
        self.items = []  # list of (iid, values)
        self.deleted = []

    def get_children(self):
        return [iid for iid, _ in self.items]

    def delete(self, iid):
        self.items = [(i, v) for i, v in self.items if i != iid]
        self.deleted.append(iid)

    def insert(self, parent, index, iid=None, values=(), tags=()):
        actual_iid = iid or f"row_{len(self.items)}"
        self.items.append((actual_iid, list(values)))
        return actual_iid

    def selection(self):
        return [self.items[0][0]] if self.items else []

    def item(self, iid, option=None):
        for i, v in self.items:
            if i == iid:
                if option == "values":
                    return v
                return {"values": v}
        return {} if option is None else []


class FakeApp:
    def __init__(self):
        self._sess = object()
        self._user = {"student_id": "2403740"}
        self._busy = False
        self.after_calls = []
        self._score_year_var = FakeVar("20252026")
        self._score_term_var = FakeVar("2")
        self._score_edit_tree = FakeTree()
        self._score_edit_status_label = FakeLabel()
        self._online_save = False
        self._score_edit_mode_label = FakeLabel()

    def after(self, delay, callback, *args):
        self.after_calls.append((delay, callback, args))

    def update_idletasks(self):
        pass

    def _update_score_tree(self, *args, **kwargs):
        pass

    def _apply_probe_result(self, *args, **kwargs):
        pass

    def _refresh_score_items(self, *args, **kwargs):
        pass

    def _on_save_done(self, *args, **kwargs):
        # 仅用于 worker 测试：worker 通过 after 调度，after 会捕获 (delay, cb, args)
        # 实际不真正执行 cb；这里作为方法存在使得 self._on_save_done 可被引用
        pass


def test_score_edit_worker_calls_after_with_items(monkeypatch):
    """worker 在后台拉数据后通过 after(0, callback, items) 回主线程。"""
    app = FakeApp()

    fake_items = [
        {
            "category": "思想成长与引领",
            "item_name": "必修-思想政治教育活动（讲座）",
            "score": 0.2,
            "limit": 1.0,
            "item_id": "12",
            "source_module_id": "2",
        },
        {
            "category": "职业精神与素质养成",
            "item_name": "必修-劳动教育",
            "score": 0.3,
            "limit": 1.0,
            "item_id": "34",
            "source_module_id": "4",
        },
    ]

    monkeypatch.setattr(
        secondclass_auto_score_gui.secondclass_tool,
        "fetch_score_items_with_session",
        lambda sess, year_id, term_id: fake_items,
    )

    secondclass_auto_score_gui.App._refresh_score_items_worker(app)

    # 应已调度 after(0, _update_score_tree, items)
    assert len(app.after_calls) >= 1
    delay, callback, args = app.after_calls[0]
    assert delay == 0
    assert args[0] == fake_items


def test_update_score_tree_inserts_items_into_treeview():
    """_update_score_tree 应清空并填充 Treeview。"""
    app = FakeApp()
    items = [
        {
            "category": "思想成长与引领",
            "item_name": "必修-思想政治教育活动（讲座）",
            "score": 0.2,
            "limit": 1.0,
            "item_id": "12",
        },
        {
            "category": "职业精神与素质养成",
            "item_name": "必修-劳动教育",
            "score": 0.3,
            "limit": 1.0,
            "item_id": "34",
        },
    ]

    secondclass_auto_score_gui.App._update_score_tree(app, items)

    assert len(app._score_edit_tree.items) == 2
    first_values = app._score_edit_tree.items[0][1]
    assert "必修-思想政治教育活动（讲座）" in first_values
    assert 0.2 in first_values


def test_probe_score_endpoints_sets_online_save_flag(monkeypatch):
    """_probe_score_endpoints_action 探测后写 self._online_save。"""
    app = FakeApp()

    monkeypatch.setattr(
        secondclass_auto_score_gui.secondclass_tool,
        "probe_score_edit_endpoints",
        lambda sess: {"online_available": False, "edit_page": "", "save_endpoint": ""},
    )

    secondclass_auto_score_gui.App._probe_score_endpoints_worker(app)

    # worker 通过 after(0, ...) 更新 UI
    assert any(
        callable(cb) for _, cb, _ in app.after_calls
    )
    # 最终标志应为 False（离线模式）
    # callback 应被给出含 online_available 的 dict
    found_dict = False
    for _, _, args in app.after_calls:
        if args and isinstance(args[0], dict) and "online_available" in args[0]:
            found_dict = True
            break
    assert found_dict


def test_apply_probe_result_updates_online_flag_and_label():
    """_apply_probe_result 应根据探测结果设置 _online_save 和 mode label。"""
    app = FakeApp()

    secondclass_auto_score_gui.App._apply_probe_result(
        app, {"online_available": False, "edit_page": "", "save_endpoint": ""}
    )

    assert app._online_save is False
    assert any("本地" in (call.get("text") or "") for call in app._score_edit_mode_label.calls)


def test_apply_probe_result_online_when_endpoint_found():
    app = FakeApp()

    secondclass_auto_score_gui.App._apply_probe_result(
        app,
        {
            "online_available": True,
            "edit_page": "",
            "save_endpoint": "https://x/saveScore.html",
        },
    )

    assert app._online_save is True
    assert any("在线" in (call.get("text") or "") for call in app._score_edit_mode_label.calls)


# ──────────────────────────────────────────────────────────────────────
# 保存逻辑（worker + on_save_done）
# ──────────────────────────────────────────────────────────────────────


def test_save_worker_local_calls_save_score_item_local(monkeypatch):
    """force_local=True 或 _online_save=False 时走本地写入。"""
    app = FakeApp()
    app._online_save = False

    called = {}

    def fake_save(student_id, item, new_score):
        called["args"] = (student_id, item, new_score)
        return {
            "success": True,
            "mode": "local",
            "field": "labor_score",
            "old_score": 0.3,
            "new_score": 0.4,
        }

    monkeypatch.setattr(
        secondclass_auto_score_gui.secondclass_tool,
        "save_score_item_local",
        fake_save,
    )

    item = {"item_name": "必修-劳动教育", "category": "职业精神与素质养成"}
    secondclass_auto_score_gui.App._save_score_item_worker(
        app, "2403740", item, 0.4, False
    )

    assert called["args"] == ("2403740", item, 0.4)
    # 应通过 after(0, _on_save_done, result) 调度
    assert any(
        args and isinstance(args[0], dict) and args[0].get("success") is True
        for _, _, args in app.after_calls
    )


def test_save_worker_force_local_ignores_online_flag(monkeypatch):
    """force_local=True 即使 _online_save=True 也走本地。"""
    app = FakeApp()
    app._online_save = True  # 即便服务器在线
    called = {"local": False, "online": False}

    def fake_local(student_id, item, new_score):
        called["local"] = True
        return {"success": True, "mode": "local", "field": "labor_score",
                "old_score": 0, "new_score": new_score}

    monkeypatch.setattr(
        secondclass_auto_score_gui.secondclass_tool,
        "save_score_item_local",
        fake_local,
    )

    item = {"item_name": "必修-劳动教育"}
    secondclass_auto_score_gui.App._save_score_item_worker(
        app, "2403740", item, 0.4, True  # force_local=True
    )

    assert called["local"] is True


def test_save_worker_online_returns_unavailable_when_no_endpoint(monkeypatch):
    """_online_save=True 但服务器无写入端点（当前实际情况）应返回失败信息，不抛异常。"""
    app = FakeApp()
    app._online_save = True

    def boom(*a, **kw):
        raise AssertionError("local 不该被调用（在线分支）")

    monkeypatch.setattr(
        secondclass_auto_score_gui.secondclass_tool,
        "save_score_item_local",
        boom,
    )

    item = {"item_name": "必修-劳动教育"}
    secondclass_auto_score_gui.App._save_score_item_worker(
        app, "2403740", item, 0.4, False  # force_local=False
    )

    # on_save_done 应被调度，且 success=False
    found = False
    for _, _, args in app.after_calls:
        if args and isinstance(args[0], dict) and args[0].get("success") is False:
            found = True
            break
    assert found


def test_on_save_done_success_updates_status_and_schedules_refresh():
    """成功保存应更新状态标签并 200-500ms 内调度刷新。"""
    app = FakeApp()

    secondclass_auto_score_gui.App._on_save_done(
        app,
        {
            "success": True,
            "mode": "local",
            "field": "labor_score",
            "old_score": 0.3,
            "new_score": 0.4,
        },
    )

    # 状态标签应被 configure（含成功提示）
    assert any(
        "0.3" in (call.get("text") or "") or "0.4" in (call.get("text") or "")
        for call in app._score_edit_status_label.calls
    )
    # 应调度 _refresh_score_items
    assert any(delay > 0 for delay, _, _ in app.after_calls)


def test_on_save_done_failure_shows_error_message():
    app = FakeApp()

    secondclass_auto_score_gui.App._on_save_done(
        app,
        {"success": False, "mode": "online", "message": "服务器未开放写入端点"},
    )

    assert any(
        "服务器未开放写入端点" in (call.get("text") or "")
        for call in app._score_edit_status_label.calls
    )
