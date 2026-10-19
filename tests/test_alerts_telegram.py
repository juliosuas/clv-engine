"""Telegram/webhook sinks and `test-alert`. All HTTP is mocked; sockets are blocked by conftest."""

import io
import json
import logging
import urllib.error
from pathlib import Path

import pytest

from clv_engine import cli
from clv_engine.alerts import (Alert, AlertManager, AlertSendError, TelegramSink, WebhookSink, build_sinks,
                               make_alerts, redact, send_test_alert)
from clv_engine.config import Settings

TOKEN = "123456789:AAH-fake_TOKEN_for_tests_only"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
ROOT = Path(__file__).resolve().parents[1]


class Resp:
    def __init__(self, body=b'{"ok":true,"result":{}}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def http_error(code, desc="Bad Request: chat not found"):
    return urllib.error.HTTPError(URL, code, "err", {}, io.BytesIO(json.dumps({"ok": False, "description": desc}).encode()))


class Opener:
    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def __call__(self, req, timeout):
        self.calls.append({"url": req.full_url, "method": req.get_method(), "timeout": timeout, "body": json.loads(req.data)})
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def tg(opener, **kw):
    return TelegramSink(TOKEN, "-1001234", opener=opener, sleep=lambda s: None, **kw)


def test_telegram_sendmessage_plain_text():
    op = Opener(Resp())
    tg(op).send(Alert("arb", "k", "PM vs K *Am\u00e9rica* [x](y) +$2.10_", "check rules"))
    (c,) = op.calls
    assert c["url"] == URL and c["method"] == "POST" and c["timeout"] == 10.0
    assert c["body"]["chat_id"] == "-1001234"
    assert c["body"]["text"] == "[arb] PM vs K *Am\u00e9rica* [x](y) +$2.10_\ncheck rules"
    assert "parse_mode" not in c["body"]


@pytest.mark.parametrize("first", [urllib.error.URLError("timed out"), TimeoutError(), http_error(503), http_error(429)])
def test_one_retry_on_transient(first):
    op = Opener(first, Resp())
    tg(op).send(Alert("system", "k", "t"))
    assert len(op.calls) == 2


def test_gives_up_after_one_retry_and_redacts():
    op = Opener(urllib.error.URLError(f"cannot reach {URL}"), urllib.error.URLError(f"again {TOKEN}"), Resp())
    with pytest.raises(AlertSendError) as ei:
        tg(op).send(Alert("system", "k", "t"))
    assert len(op.calls) == 2
    assert TOKEN not in str(ei.value) and "AAH-fake" not in str(ei.value)


def test_4xx_not_retried_and_no_token():
    op = Opener(http_error(401, "Unauthorized"), Resp())
    with pytest.raises(AlertSendError) as ei:
        tg(op).send(Alert("system", "k", "t"))
    assert len(op.calls) == 1 and str(ei.value) == "HTTP 401: Unauthorized"
    assert ei.value.__suppress_context__ and ei.value.__cause__ is None


def test_ok_false_fails():
    with pytest.raises(AlertSendError, match="ok=false"):
        tg(Opener(Resp(b'{"ok":false,"description":"nope"}'))).send(Alert("system", "k", "t"))


def test_bad_token_rejected_without_echo():
    with pytest.raises(ValueError) as ei:
        TelegramSink("not a token/../getUpdates", "1")
    assert "getUpdates" not in str(ei.value)


def test_repr_and_manager_log_never_leak(caplog):
    s = tg(Opener(http_error(401, "Unauthorized")))
    assert TOKEN not in repr(s)
    with caplog.at_level(logging.WARNING):
        assert AlertManager([s]).emit(Alert("arb", "k", "t"))
    assert "HTTP 401" in caplog.text and TOKEN not in caplog.text


def test_webhook_raises_instead_of_swallowing():
    op = Opener(http_error(404, ""))
    with pytest.raises(AlertSendError, match="HTTP 404"):
        WebhookSink("https://hooks.example.com/secret", opener=op, sleep=lambda s: None).send(Alert("system", "k", "t"))
    assert "secret" not in repr(WebhookSink("https://hooks.example.com/secret"))


def test_redact():
    assert redact(f"POST {URL}") == "POST https://api.telegram.org/bot***/sendMessage"


def test_env_names_and_both_required(monkeypatch):
    for k in ("ALERT_SINKS", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f" {TOKEN} ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234")
    s = Settings.from_env(dotenv=None)
    assert s.telegram_bot_token == TOKEN and TOKEN not in repr(s)
    # on by itself (ALERT_SINKS default stdout,file doesn't list telegram)
    assert [type(x).__name__ for x in make_alerts(s).sinks] == ["StdoutSink", "FileSink", "TelegramSink"]
    for tok, chat in ((TOKEN, ""), ("", "1")):
        st = Settings(alert_sinks=["stdout", "telegram"], telegram_bot_token=tok, telegram_chat_id=chat)
        assert [type(x).__name__ for x in build_sinks(st)] == ["StdoutSink"]


def test_bad_token_skipped_at_runtime_strict_raises(caplog):
    st = Settings(alert_sinks=[], telegram_bot_token="bad token", telegram_chat_id="1")
    with caplog.at_level(logging.ERROR):
        assert build_sinks(st) == []
    assert "bad token" not in caplog.text
    with pytest.raises(ValueError):
        build_sinks(st, strict=True)


def test_send_test_alert_results():
    class Ok:
        def __init__(self):
            self.got = []

        def send(self, a):
            self.got.append(a)

    ok = Ok()
    res = send_test_alert([ok, tg(Opener(http_error(400)))], "hello")
    assert res == {"Ok": None, "telegram": "HTTP 400: Bad Request: chat not found"}
    assert ok.got[0].data["test"] is True


def _env(monkeypatch, tmp_path, **extra):
    for k in ("ALERT_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ALERT_SINKS", "stdout,file")
    monkeypatch.setenv("ALERT_FILE", str(tmp_path / "alerts.jsonl"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "clv.sqlite"))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_cli_test_alert_ok(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="42")
    op = Opener(Resp())
    monkeypatch.setattr("urllib.request.urlopen", op)
    assert cli.main(["--env-file", "/dev/null", "test-alert"]) == 0
    out = capsys.readouterr().out
    assert "telegram: ok" in out and "file: ok" in out and "stdout: ok" in out and TOKEN not in out
    assert "test alert" in op.calls[0]["body"]["text"] and op.calls[0]["body"]["chat_id"] == "42"
    assert not (tmp_path / "clv.sqlite").exists()  # test-alert never touches the ledger


def test_cli_test_alert_failure_exit_1(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path, TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="42", ALERT_WEBHOOK_URL="https://hooks.example.com/x")
    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if "telegram" in req.full_url:
            raise http_error(401, "Unauthorized")
        return Resp(b"ok")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert cli.main(["--env-file", "/dev/null", "test-alert"]) == 1
    cap = capsys.readouterr()
    assert "webhook: ok" in cap.out and "telegram: FAILED (HTTP 401: Unauthorized)" in cap.out
    assert TOKEN not in cap.out + cap.err and sum("telegram" in u for u in calls) == 1


def test_systemd_unit_uses_magno_env_path():
    unit = (ROOT / "deploy" / "clv-engine.service").read_text()
    assert "EnvironmentFile=/etc/clv-engine/clv-engine.env" in unit
