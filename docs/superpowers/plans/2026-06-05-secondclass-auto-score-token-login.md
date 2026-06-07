# Secondclass Auto Score Token Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the Windows batch launcher and change the auto-score GUI so users only enter `access_token`, then the app converts it to `SSID` before connecting.

**Architecture:** Keep authentication logic centralized in `secondclass.secondclass_tool.convert_to_ssid`. The GUI owns only Tkinter input/state handling and session validation. The batch file becomes ASCII-only to avoid Windows codepage corruption.

**Tech Stack:** Python 3, Tkinter, pytest, Windows batch, existing `secondclass.secondclass_tool` requests-based auth helpers.

---

## File Structure

- Modify: `secondclass_auto_score.bat`
  - Responsibility: Windows launcher/menu for `secondclass_auto_score.py`.
  - Change: Replace Chinese menu text with ASCII-only text and use a stable codepage declaration.

- Modify: `secondclass_auto_score_gui.py`
  - Responsibility: Tkinter GUI for auto-score operations.
  - Change: Replace top SSID input with access_token input; convert token to SSID on connect; then validate session.

- Modify: `tests/test_secondclass_tool.py`
  - Responsibility: Existing secondclass auth and utility tests.
  - Change: Add token-only `convert_to_ssid` regression test.

- Create: `tests/test_secondclass_auto_score_gui.py`
  - Responsibility: GUI method-level tests without opening a real mainloop.
  - Change: Verify `_connect` uses access_token conversion and validates via returned SSID.

---

### Task 1: Add token-only convert_to_ssid regression test

**Files:**
- Modify: `tests/test_secondclass_tool.py`
- Test: `tests/test_secondclass_tool.py`

- [ ] **Step 1: Write the failing test**

Append this test after `test_obtain_portal_ticket_returns_empty_for_no_token` in `tests/test_secondclass_tool.py`:

```python
def test_convert_to_ssid_with_token_only_refreshes_ticket_and_returns_ssid(monkeypatch):
    class CookieJar(dict):
        def get(self, key, default=None):
            return super().get(key, default)

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

    sessions = []

    def fake_session_factory():
        sess = BridgeSession()
        sessions.append(sess)
        return sess

    monkeypatch.setattr(secondclass_tool, "_obtain_portal_ticket", lambda token: "fresh-ticket")
    monkeypatch.setattr(secondclass_tool.requests, "Session", fake_session_factory)

    ssid = secondclass_tool.convert_to_ssid(access_token="token-secret")

    assert ssid == "token-ssid"
    assert sessions[0].trust_env is False
    assert any(u == "http://szxy.cqtbi.edu.cn/cqdddt/dtLog!log.action" for u, _ in sessions[0].post_calls)
    assert any(u == "https://2class.cqtbi.edu.cn/Admin/Index/cqtbiSSO" for u, _ in sessions[0].get_calls)
    assert any(p["params"] == {"PORTAL_TICKET": "fresh-ticket"} for _, p in sessions[0].get_calls)
```

- [ ] **Step 2: Run test to verify current behavior**

Run:

```bash
python -m pytest tests/test_secondclass_tool.py::test_convert_to_ssid_with_token_only_refreshes_ticket_and_returns_ssid -v
```

Expected: PASS if token-only path already works; FAIL if the existing conversion path rejects token-only credentials. If it passes, do not change `secondclass/secondclass_tool.py` for this task.

- [ ] **Step 3: Minimal implementation only if the test fails**

If the test fails because `obtain_secondclass_session_from_user` rejects `expires_at=0` or token-only credentials, modify only `secondclass/secondclass_tool.py:828-837` to keep token-only refresh allowed:

```python
    if expires_at and expires_at < int(time.time()) and not access_token:
        raise SecondClassAuthError("登录已过期，请重新 #扫码登录")
    if not portal_ticket and not access_token:
        raise SecondClassAuthError("缺少登录凭证，请重新 #扫码登录")

    if not portal_ticket:
        portal_ticket = _obtain_portal_ticket(access_token)
        if not portal_ticket:
            raise SecondClassAuthError("缺少门户票据，请重新 #扫码登录")
```

- [ ] **Step 4: Run the focused test again**

Run:

```bash
python -m pytest tests/test_secondclass_tool.py::test_convert_to_ssid_with_token_only_refreshes_ticket_and_returns_ssid -v
```

Expected: PASS.

---

### Task 2: Add GUI connect behavior test

**Files:**
- Create: `tests/test_secondclass_auto_score_gui.py`
- Modify: `secondclass_auto_score_gui.py`
- Test: `tests/test_secondclass_auto_score_gui.py`

- [ ] **Step 1: Write the failing GUI method test**

Create `tests/test_secondclass_auto_score_gui.py` with this content:

```python
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
    def __init__(self, token="token-secret"):
        self._access_token_var = FakeVar(token)
        self._status_label = FakeLabel()
        self._sess = None
        self._user = {}
        self.after_calls = []
        self.idle_updated = False

    def update_idletasks(self):
        self.idle_updated = True

    def after(self, delay, callback):
        self.after_calls.append((delay, callback))


def test_connect_converts_access_token_to_ssid_and_validates_session(monkeypatch):
    app = FakeApp()
    converted = []

    class FakeSession:
        def __init__(self, ssid):
            self.ssid = ssid
            self.get_calls = []

        def get(self, url, timeout):
            self.get_calls.append((url, timeout))
            return type("Resp", (), {"status_code": 200, "text": "ok"})()

    def fake_convert_to_ssid(*, access_token):
        converted.append(access_token)
        return "ssid-from-token"

    sessions = []

    def fake_session(ssid):
        sess = FakeSession(ssid)
        sessions.append(sess)
        return sess

    monkeypatch.setattr(secondclass_auto_score_gui.secondclass_tool, "convert_to_ssid", fake_convert_to_ssid)
    monkeypatch.setattr(secondclass_auto_score_gui.secondclass_tool, "_session", fake_session)

    secondclass_auto_score_gui.App._connect(app)

    assert converted == ["token-secret"]
    assert app._sess is sessions[0]
    assert sessions[0].ssid == "ssid-from-token"
    assert app._user == {"student_id": "", "realname": ""}
    assert any(call.get("text") == "已连接" for call in app._status_label.calls)
    assert app.after_calls and app.after_calls[0][0] == 100


def test_connect_rejects_empty_access_token(monkeypatch):
    app = FakeApp(token="")
    warnings = []

    monkeypatch.setattr(
        secondclass_auto_score_gui.messagebox,
        "showwarning",
        lambda title, message: warnings.append((title, message)),
    )

    secondclass_auto_score_gui.App._connect(app)

    assert warnings == [("提示", "请输入access_token")]
    assert app._sess is None
```

- [ ] **Step 2: Run test to verify it fails before GUI implementation**

Run:

```bash
python -m pytest tests/test_secondclass_auto_score_gui.py -v
```

Expected: FAIL because `App` still uses `_ssid_var` and does not call `convert_to_ssid`.

- [ ] **Step 3: Rename GUI state variable in `__init__`**

Modify `secondclass_auto_score_gui.py:69-72` from SSID state to access token state:

```python
        self._sess = None
        self._user = {}
        self._access_token_var = tk.StringVar()
        self._busy = False
```

- [ ] **Step 4: Replace top input UI labels and binding**

Modify `secondclass_auto_score_gui.py:107-123` to this:

```python
        token_frame = tk.Frame(self, bg=COLORS["card"], pady=6, padx=12)
        token_frame.pack(fill="x", padx=12, pady=(8, 0))

        tk.Label(
            token_frame,
            text="access_token:",
            bg=COLORS["card"],
            font=("Consolas", 10),
        ).pack(side="left")
        token_entry = ttk.Entry(
            token_frame, textvariable=self._access_token_var, width=48, font=("Consolas", 10)
        )
        token_entry.pack(side="left", padx=8)
        ttk.Button(token_frame, text="连接", command=self._connect, width=8).pack(
            side="left", padx=4
        )
        self._status_label = tk.Label(
            token_frame,
            text="未连接",
            bg=COLORS["card"],
            fg=COLORS["danger"],
            font=("Microsoft YaHei UI", 9),
        )
        self._status_label.pack(side="left", padx=12)
```

- [ ] **Step 5: Replace `_connect` implementation**

Modify `secondclass_auto_score_gui.py:601-624` to this:

```python
    def _connect(self):
        access_token = self._access_token_var.get().strip()
        if not access_token:
            messagebox.showwarning("提示", "请输入access_token")
            return

        self._status_label.configure(text="转换SSID中...", fg=COLORS["warning"])
        self.update_idletasks()

        ssid = secondclass_tool.convert_to_ssid(access_token=access_token)
        if not ssid:
            self._status_label.configure(text="SSID转换失败", fg=COLORS["danger"])
            log.error("access_token 换 SSID 失败")
            return

        self._status_label.configure(text="连接中...", fg=COLORS["warning"])
        self.update_idletasks()

        sess = secondclass_tool._session(ssid)
        try:
            r = sess.get(
                f"{secondclass_tool.BASE_URL}/Student/My/index.html", timeout=10
            )
            if r.status_code == 200 and "top.location.href" not in r.text:
                self._sess = sess
                self._user = {"student_id": "", "realname": ""}
                self._status_label.configure(text="已连接", fg=COLORS["success"])
                self.after(100, self._refresh_dashboard)
            else:
                self._status_label.configure(text="SSID已过期", fg=COLORS["danger"])
        except Exception as e:
            self._status_label.configure(text="连接失败", fg=COLORS["danger"])
            log.error("连接失败: %s", e)
```

- [ ] **Step 6: Update session warning text**

Modify `secondclass_auto_score_gui.py:626-630` to this:

```python
    def _ensure_session(self) -> bool:
        if self._sess is not None:
            return True
        messagebox.showwarning("提示", "请先输入access_token并点击连接")
        return False
```

- [ ] **Step 7: Run GUI tests**

Run:

```bash
python -m pytest tests/test_secondclass_auto_score_gui.py -v
```

Expected: PASS.

---

### Task 3: Fix Windows batch menu encoding risk

**Files:**
- Modify: `secondclass_auto_score.bat`
- Test: manual command via Windows `cmd`

- [ ] **Step 1: Replace batch file with ASCII-only content**

Replace all content of `secondclass_auto_score.bat` with:

```bat
@echo off
chcp 65001 >nul
title Secondclass Auto Score - 2403740

set "DIR=%~dp0"
set "PY=%DIR%venv\Scripts\python.exe"
set "SCRIPT=%DIR%secondclass_auto_score.py"

if not "%~1"=="" goto run_args

goto menu

:run_args
"%PY%" "%SCRIPT%" %*
if errorlevel 1 pause
goto :eof

:menu
cls
echo ========================================
echo  Secondclass Auto Score - 2403740
echo ========================================
echo.
echo  1. Show score dashboard
echo  2. Signup dry-run preview
echo  3. Signup for real
echo  4. Probe action endpoints
echo  5. Start monitor
echo  6. Full auto mode
echo  7. access_token / ticket to SSID
echo  0. Exit
echo.
set /p "choice=Select: "

if "%choice%"=="1" "%PY%" "%SCRIPT%" status & pause & goto menu
if "%choice%"=="2" "%PY%" "%SCRIPT%" signup --dry-run --max 20 & pause & goto menu
if "%choice%"=="3" "%PY%" "%SCRIPT%" signup --max 10 & pause & goto menu
if "%choice%"=="4" "%PY%" "%SCRIPT%" probe & pause & goto menu
if "%choice%"=="5" "%PY%" "%SCRIPT%" monitor & pause & goto menu
if "%choice%"=="6" "%PY%" "%SCRIPT%" run --max-signup 10 & pause & goto menu
if "%choice%"=="7" "%PY%" "%SCRIPT%" ticket --json "%DIR%accounts.json" --student-id 2403740 & pause & goto menu
if "%choice%"=="0" exit /b 0
goto menu
```

- [ ] **Step 2: Verify argument path does not enter menu**

Run:

```bash
cmd /c secondclass_auto_score.bat --help
```

Expected: The Python argparse help prints, and no batch syntax error appears.

- [ ] **Step 3: Verify menu can render**

Run this manually in an interactive Windows command prompt if available:

```bat
secondclass_auto_score.bat
```

Expected: Menu shows ASCII text and accepts `0` to exit without messages like `'n.exe' is not recognized`, `'*' is not recognized`, or broken `echo` commands.

---

### Task 4: Run focused regression suite

**Files:**
- Test only

- [ ] **Step 1: Run secondclass tool auth tests**

Run:

```bash
python -m pytest tests/test_secondclass_tool.py::test_obtain_secondclass_session_uses_cqtbi_sso_endpoint tests/test_secondclass_tool.py::test_obtain_portal_ticket_returns_empty_for_no_token tests/test_secondclass_tool.py::test_convert_to_ssid_with_token_only_refreshes_ticket_and_returns_ssid -v
```

Expected: PASS.

- [ ] **Step 2: Run GUI tests**

Run:

```bash
python -m pytest tests/test_secondclass_auto_score_gui.py -v
```

Expected: PASS.

- [ ] **Step 3: Run formatting/lint if available**

Run:

```bash
python -m ruff format secondclass_auto_score_gui.py tests/test_secondclass_auto_score_gui.py tests/test_secondclass_tool.py
python -m ruff check secondclass_auto_score_gui.py tests/test_secondclass_auto_score_gui.py tests/test_secondclass_tool.py
```

Expected: Both commands complete without errors. If `ruff` is not installed, report that and rely on pytest plus manual review.

- [ ] **Step 4: Final smoke check for imports**

Run:

```bash
python -c "import secondclass_auto_score_gui; import secondclass_auto_score; from secondclass import secondclass_tool; print('ok')"
```

Expected: `ok`.

---

## Self-Review

- Spec coverage: batch encoding fix is covered by Task 3; GUI access_token-only connection is covered by Task 2; shared token-to-SSID path is covered by Task 1; verification is covered by Task 4.
- Placeholder scan: no `TBD`, `TODO`, or vague implementation steps remain.
- Type consistency: GUI variable is consistently `_access_token_var`; conversion API is consistently `secondclass_tool.convert_to_ssid(access_token=...)`; session validation still uses `secondclass_tool._session(ssid)`.
- User constraint: no git commit steps are included because the user said no git and this directory is not a git repository.
