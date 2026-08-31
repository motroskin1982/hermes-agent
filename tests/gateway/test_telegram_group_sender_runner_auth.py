from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.gateway.test_telegram_auth_check import _make_adapter


def _runner_with(adapter):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    object.__setattr__(runner, "pairing_store", None)
    object.__setattr__(runner, "pairing_stores", {})
    return runner


def _source(*, chat_id: str, user_id: str, chat_type: str = "group"):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
    )


def test_exact_group_sender_policy_survives_stale_global_env(monkeypatch):
    """Adapter's exact chat+sender policy must win over stale service env.

    Regression: Telegram intake accepted a partner via groups.<chat>.allow_from,
    then GatewayRunner rejected the same event because systemd still exposed an
    owner-only TELEGRAM_ALLOWED_USERS / TELEGRAM_GROUP_ALLOWED_USERS list.
    """
    ruta_chat = "-1004408727557"
    partner = "1262759322"
    owner = "owner-id"
    adapter = _make_adapter(
        allow_from=[owner],
        groups={ruta_chat: {"allow_from": [owner, partner]}},
    )
    runner = _runner_with(adapter)

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", owner)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", owner)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-100111,-100222")
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    assert runner._is_user_authorized(
        _source(chat_id=ruta_chat, user_id=partner)
    ) is True
    assert runner._is_user_authorized(
        _source(chat_id="-100999", user_id=partner)
    ) is False
    assert runner._is_user_authorized(
        _source(chat_id=partner, user_id=partner, chat_type="dm")
    ) is False
