"""Configuration loading."""

import os
from decimal import Decimal

import pytest

from uotpbot.config import ConfigError, from_environment, load_env_file
from uotpbot.money import INR


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate each test from the ambient environment and from each other."""
    for key in list(os.environ):
        if key.startswith(("UOTP_", "FEE_", "ENGINE_", "TELEGRAM_", "LEDGER_", "PRICES_")):
            monkeypatch.delenv(key, raising=False)


def test_api_key_is_required(monkeypatch):
    with pytest.raises(ConfigError, match="UOTP_API_KEY"):
        from_environment(env_file="/nonexistent/.env")


def test_defaults_are_sane(monkeypatch):
    monkeypatch.setenv("UOTP_API_KEY", "secret")
    s = from_environment(env_file="/nonexistent/.env")
    assert s.uotp.api_key == "secret"
    assert s.uotp.base_url == "https://uotp.store/api/stubs/handler_api.php"
    assert s.uotp.action_balance == "getBalance"
    assert s.uotp.balance_prefix == "ACCESS_BALANCE"
    assert s.uotp.balance_divisor == Decimal(1)
    assert s.fees.gateway_rate == Decimal("0.02")
    assert s.fees.gst_rate == Decimal(0)
    assert s.fees.gst_inclusive is True
    assert s.engine.retry_cap == 3
    assert s.engine.default_country == "22"  # uotp.store handler_api: India
    assert not s.has_telegram


def test_overrides_apply(monkeypatch):
    monkeypatch.setenv("UOTP_API_KEY", "k")
    monkeypatch.setenv("FEE_GATEWAY_RATE", "0.03")
    monkeypatch.setenv("FEE_GATEWAY_FIXED", "2.50")
    monkeypatch.setenv("FEE_GST_RATE", "0.18")
    monkeypatch.setenv("FEE_GST_INCLUSIVE", "false")
    monkeypatch.setenv("FEE_CHARGEBACK_RATE", "0.02")
    monkeypatch.setenv("ENGINE_RETRY_CAP", "5")
    monkeypatch.setenv("ENGINE_OTP_TIMEOUT", "120")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1, 2 ,3")
    s = from_environment(env_file="/nonexistent/.env")
    assert s.fees.gateway_rate == Decimal("0.03")
    assert s.fees.gateway_fixed == INR("2.50")
    assert s.fees.gst_rate == Decimal("0.18")
    assert s.fees.gst_inclusive is False
    assert s.fees.chargeback_rate == Decimal("0.02")
    assert s.engine.retry_cap == 5
    assert s.engine.otp_timeout_seconds == 120.0
    assert s.has_telegram
    assert s.allowed_users == ("1", "2", "3")


def test_protocol_vocabulary_is_configurable(monkeypatch):
    """Action names and response prefixes are config, not code."""
    monkeypatch.setenv("UOTP_API_KEY", "k")
    monkeypatch.setenv("UOTP_BASE_URL", "https://example.test/api.php")
    monkeypatch.setenv("UOTP_ACTION_BALANCE", "balance")
    monkeypatch.setenv("UOTP_PREFIX_BALANCE", "WALLET_BALANCE")
    monkeypatch.setenv("UOTP_ACTION_GET_NUMBER", "getNum")
    monkeypatch.setenv("UOTP_PREFIX_NUMBER", "NUM")
    monkeypatch.setenv("UOTP_STATUS_CANCEL", "9")
    monkeypatch.setenv("UOTP_BALANCE_DIVISOR", "100")
    monkeypatch.setenv("UOTP_KEY_PARAM", "key")
    s = from_environment(env_file="/nonexistent/.env")
    assert s.uotp.base_url == "https://example.test/api.php"
    assert s.uotp.action_balance == "balance"
    assert s.uotp.balance_prefix == "WALLET_BALANCE"
    assert s.uotp.action_get_number == "getNum"
    assert s.uotp.number_prefix == "NUM"
    assert s.uotp.status_cancel == "9"
    assert s.uotp.balance_divisor == Decimal(100)
    assert s.uotp.key_param == "key"


def test_bad_decimal_is_reported(monkeypatch):
    monkeypatch.setenv("UOTP_API_KEY", "k")
    monkeypatch.setenv("FEE_GATEWAY_RATE", "not-a-number")
    with pytest.raises(ConfigError, match="not a valid decimal"):
        from_environment(env_file="/nonexistent/.env")


def test_require_telegram_raises_without_a_token(monkeypatch):
    monkeypatch.setenv("UOTP_API_KEY", "k")
    s = from_environment(env_file="/nonexistent/.env")
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        s.require_telegram()


def test_env_file_is_parsed(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "# a comment\n"
        "\n"
        "UOTP_API_KEY=from-file\n"
        'FEE_GATEWAY_RATE="0.05"\n'
        "export ENGINE_RETRY_CAP=7\n"
        "MALFORMED LINE WITHOUT EQUALS\n",
        encoding="utf-8",
    )
    loaded = load_env_file(path)
    assert loaded["UOTP_API_KEY"] == "from-file"
    s = from_environment(env_file=path)
    assert s.uotp.api_key == "from-file"
    assert s.fees.gateway_rate == Decimal("0.05")
    assert s.engine.retry_cap == 7


def test_env_file_does_not_override_real_environment(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("UOTP_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("UOTP_API_KEY", "from-env")
    s = from_environment(env_file=path)
    assert s.uotp.api_key == "from-env"


def test_missing_env_file_is_not_an_error():
    assert load_env_file("/definitely/not/here/.env") == {}


def test_prices_params_and_service_map(monkeypatch):
    monkeypatch.setenv("UOTP_API_KEY", "k")
    monkeypatch.setenv("UOTP_PRICES_COUNTRY", "182")
    monkeypatch.setenv("UOTP_PRICES_OPERATOR", "jiotel")
    monkeypatch.setenv("UOTP_SERVICE_MAP", "whatsapp=wa, telegram=tg ,google=go")
    s = from_environment(env_file="/nonexistent/.env")
    assert s.uotp.prices_country == "182"
    assert s.uotp.prices_operator == "jiotel"
    assert s.uotp.service_map == {"whatsapp": "wa", "telegram": "tg", "google": "go"}


def test_empty_service_map_parses_to_empty(monkeypatch):
    monkeypatch.setenv("UOTP_API_KEY", "k")
    s = from_environment(env_file="/nonexistent/.env")
    assert s.uotp.service_map == {}


def test_malformed_service_map_entries_are_skipped(monkeypatch):
    monkeypatch.setenv("UOTP_API_KEY", "k")
    monkeypatch.setenv("UOTP_SERVICE_MAP", "whatsapp=wa,broken,=tg,google=")
    s = from_environment(env_file="/nonexistent/.env")
    assert s.uotp.service_map == {"whatsapp": "wa"}
