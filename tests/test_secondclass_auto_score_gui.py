import secondclass_auto_score_gui


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value


class FakeLabel:
    def __init__(self):
        self.calls = []

    def configure(self, **kwargs):
        self.calls.append(kwargs)


class FakeApp:
    def __init__(self, ssid="ssid-from-user"):
        self._ssid_var = FakeVar(ssid)
        self._status_label = FakeLabel()
        self._sess = None
        self._user = {}
        self.after_calls = []
        self.idle_updated = False
        self._refresh_dashboard = lambda: None

    def update_idletasks(self):
        self.idle_updated = True

    def after(self, delay, callback):
        self.after_calls.append((delay, callback))


def test_connect_uses_ssid_directly_and_validates_session(monkeypatch):
    app = FakeApp(ssid="raw-ssid")

    class FakeSession:
        def __init__(self, ssid):
            self.ssid = ssid
            self.get_calls = []

        def get(self, url, timeout):
            self.get_calls.append((url, timeout))
            return type("Resp", (), {"status_code": 200, "text": "ok"})()

    sessions = []

    def fake_session(ssid):
        sess = FakeSession(ssid)
        sessions.append(sess)
        return sess

    # convert_to_ssid 不应再被调用
    def boom(*a, **kw):
        raise AssertionError("convert_to_ssid should not be called when SSID is provided")

    monkeypatch.setattr(
        secondclass_auto_score_gui.secondclass_tool, "convert_to_ssid", boom
    )
    monkeypatch.setattr(
        secondclass_auto_score_gui.secondclass_tool, "_session", fake_session
    )

    secondclass_auto_score_gui.App._connect(app)

    assert app._sess is sessions[0]
    assert sessions[0].ssid == "raw-ssid"
    assert app._user == {"student_id": "", "realname": ""}
    assert any(call.get("text") == "已连接" for call in app._status_label.calls)
    assert app.after_calls and app.after_calls[0][0] == 100


def test_connect_rejects_empty_ssid(monkeypatch):
    app = FakeApp(ssid="")
    warnings = []

    monkeypatch.setattr(
        secondclass_auto_score_gui.messagebox,
        "showwarning",
        lambda title, message: warnings.append((title, message)),
    )

    secondclass_auto_score_gui.App._connect(app)

    assert warnings == [("提示", "请输入SSID")]
    assert app._sess is None


def test_connect_marks_expired_when_index_redirects(monkeypatch):
    app = FakeApp(ssid="expired-ssid")

    class FakeSession:
        def get(self, url, timeout):
            return type(
                "Resp",
                (),
                {"status_code": 200, "text": "<script>top.location.href='/login'</script>"},
            )()

    monkeypatch.setattr(
        secondclass_auto_score_gui.secondclass_tool, "_session", lambda ssid: FakeSession()
    )

    secondclass_auto_score_gui.App._connect(app)

    assert app._sess is None
    assert any(call.get("text") == "SSID已过期" for call in app._status_label.calls)
