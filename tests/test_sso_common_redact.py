from sso.sso_common import redact


def test_redact_masks_ssid_cookie_and_portal_ticket():
    text = redact(
        "SSID=abc123secret Cookie: SSID=abc123secret portal_ticket=portal-secret access_token=token-secret"
    )

    assert "abc123secret" not in text
    assert "portal-secret" not in text
    assert "token-secret" not in text
    assert "SSID=" in text
