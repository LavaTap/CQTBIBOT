import time

import pytest


class FakeClient:
    def __init__(self):
        self.group_messages = []
        self.private_messages = []

    def send_group_msg(self, group_id, message):
        self.group_messages.append((group_id, message))

    def send_private_msg(self, user_id, message):
        self.private_messages.append((user_id, message))


class ImmediateThread:
    def __init__(self, target, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        if self.args:
            self.target(*self.args)


def make_handler(monkeypatch):
    from qq import qq_forward

    monkeypatch.setattr(qq_forward.threading, "Thread", ImmediateThread)
    config = qq_forward.ForwardConfig(admin_qq=999, command_enabled=True)
    client = FakeClient()
    handler = qq_forward.CommandHandler(config, client, lambda level, msg: None)
    return qq_forward, handler, client


def test_qq_forward_imports_cleanly():
    from qq import qq_forward

    assert qq_forward.CommandHandler.SECOND_CLASS_INFO_CMD == "#二课信息"


def test_help_text_mentions_secondclass_command():
    from qq import qq_forward

    help_text = qq_forward.CommandHandler._help_text()

    assert "#二课信息" in help_text
    assert "需先登录" in help_text


def test_secondclass_command_is_public_and_replies_immediately(monkeypatch):
    qq_forward, handler, client = make_handler(monkeypatch)
    monkeypatch.setattr(qq_forward, "UserDB", lambda: type("DB", (), {"get_by_qq": lambda self, qq: None})())

    handled = handler.try_handle(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 123,
            "user_id": 456,
            "raw_message": "#二课信息",
        }
    )

    assert handled is True
    assert client.group_messages[0] == (123, "正在查询第二课堂信息…")
    assert "你还未登录" in client.group_messages[1][1]


def test_cmd_secondclass_info_rejects_expired_login(monkeypatch):
    qq_forward, handler, client = make_handler(monkeypatch)
    monkeypatch.setattr(
        qq_forward,
        "UserDB",
        lambda: type(
            "DB",
            (),
            {"get_by_qq": lambda self, qq: {"portal_ticket": "ticket", "expires_at": int(time.time()) - 1}},
        )(),
    )

    handler._cmd_secondclass_info("group", 123, 456)

    assert "登录已过期" in client.group_messages[-1][1]


def test_cmd_secondclass_info_fetches_saves_and_replies_summary(monkeypatch):
    qq_forward, handler, client = make_handler(monkeypatch)
    user = {
        "student_id": "2400001",
        "realname": "张三",
        "dept_name": "软件2401",
        "portal_ticket": "ticket",
        "expires_at": int(time.time()) + 60,
    }

    monkeypatch.setattr(qq_forward, "UserDB", lambda: type("DB", (), {"get_by_qq": lambda self, qq: user})())
    monkeypatch.setattr(
        qq_forward.secondclass_tool,
        "fetch_and_save_secondclass_info",
        lambda user_id, user: {
            "student_id": user["student_id"],
            "realname": user["realname"],
            "deptname": user.get("dept_name", ""),
            "college": "",
            "major": "",
            "total_score": 12.5,
            "activity_count": 4,
            "unsigned_count": 1,
            "unfinished_count": 2,
            "club_count": 3,
            "thought_score": 1,
            "skill_score": 2,
            "career_score": 3,
        },
    )

    handler._cmd_secondclass_info("group", 123, 456)

    assert "姓名: 张三" in client.group_messages[-1][1]
    assert "二课总分: 12.5 分" in client.group_messages[-1][1]


def test_cmd_secondclass_info_does_not_echo_secret_exception(monkeypatch):
    qq_forward, handler, client = make_handler(monkeypatch)
    user = {"portal_ticket": "ticket-secret", "expires_at": int(time.time()) + 60}

    monkeypatch.setattr(qq_forward, "UserDB", lambda: type("DB", (), {"get_by_qq": lambda self, qq: user})())

    def explode(user):
        raise RuntimeError("SSID=super-secret-cookie")

    monkeypatch.setattr(qq_forward.secondclass_tool, "obtain_secondclass_session_from_user", explode)

    handler._cmd_secondclass_info("group", 123, 456)

    reply = client.group_messages[-1][1]
    assert "super-secret-cookie" not in reply
    assert "查询二课信息失败" in reply
