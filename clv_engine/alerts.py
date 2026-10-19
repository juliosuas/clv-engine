"""Alert sinks with dedupe/cooldown (pattern from polymarket-desk api/alerts.py, MIT).

Sinks: stdout (logging), file (JSONL), webhook (Slack / Discord / generic JSON),
telegram (Bot API). Webhook URLs and tokens come from env only. These are the only
outbound POSTs the engine makes, and only to destinations you configure.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import iso, utc_now

log = logging.getLogger("clv_engine.alerts")


@dataclass
class Alert:
    kind: str  # entry | arb | soft_value | system
    key: str  # dedupe key
    title: str
    body: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        return f"[{self.kind}] {self.title}" + (f"\n{self.body}" if self.body else "")


class StdoutSink:
    def send(self, alert: Alert) -> None:
        log.info("ALERT %s", alert.text().replace("\n", " | "))


class FileSink:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, alert: Alert) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": iso(utc_now()), "kind": alert.kind, "key": alert.key, "title": alert.title, "body": alert.body, "data": alert.data}, default=str) + "\n")


class AlertSendError(RuntimeError):
    """Delivery failed. The message never contains the bot token or the webhook URL."""


_TOKEN_IN_PATH = re.compile(r"/bot[^/\s]*")
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
TELEGRAM_HOST = "api.telegram.org"
TELEGRAM_SEND_PATH = re.compile(r"^/bot[0-9]+:[A-Za-z0-9_-]+/sendMessage$")
TELEGRAM_MAX_CHARS = 4096


def redact(text: object) -> str:
    """Strip Telegram bot tokens (``/bot<token>``) from any URL or message."""
    return _TOKEN_IN_PATH.sub("/bot***", str(text))


class WebhookSink:
    """JSON POST with a timeout and one retry (timeouts, connection errors, 429, 5xx).

    Raises ``AlertSendError`` on failure; ``AlertManager`` catches it so a broken
    sink never stops the loop, and ``test-alert`` reports it.
    """

    def __init__(self, url: str, kind: str = "slack", timeout: float = 8.0, retries: int = 1,
                 retry_delay: float = 2.0, opener=None, sleep=time.sleep):
        if not url.startswith("https://"):
            raise ValueError("alert destinations must be https")
        self.url = url
        self.kind = kind
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep

    @property
    def name(self) -> str:
        return "webhook"

    def __repr__(self) -> str:  # the URL is a secret
        return f"{type(self).__name__}(kind={self.kind!r}, url=***)"

    def payload(self, alert: Alert) -> dict[str, Any]:
        if self.kind == "discord":
            return {"content": alert.text()[:1900]}
        if self.kind == "generic":
            return {"kind": alert.kind, "key": alert.key, "title": alert.title, "body": alert.body, "data": alert.data}
        return {"text": alert.text()}

    def _clean(self, text: str) -> str:
        return redact(str(text).replace(self.url, "***"))

    def _ok(self, raw: bytes) -> str | None:
        """None if the response body means success, else a reason."""
        return None

    def send(self, alert: Alert) -> None:
        body = json.dumps(self.payload(alert), default=str).encode()
        last = "unknown error"
        for attempt in range(self.retries + 1):
            if attempt:
                self.sleep(self.retry_delay)
            req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            retry = False
            try:
                with self.opener(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                bad = self._ok(raw)
                if bad is None:
                    return
                last = bad
            except urllib.error.HTTPError as exc:
                desc = ""
                try:
                    desc = json.loads(exc.read() or b"{}").get("description", "")
                except Exception:  # noqa: BLE001 - body is best effort only
                    pass
                last = f"HTTP {exc.code}" + (f": {desc}" if desc else "")
                retry = exc.code in RETRY_STATUSES
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
                last = f"{type(exc).__name__}: {getattr(exc, 'reason', None) or exc}"
                retry = True
            if not retry:
                break
        raise AlertSendError(self._clean(last)) from None  # drop HTTPError: it carries the URL


class TelegramSink(WebhookSink):
    """Telegram Bot API ``POST /bot<token>/sendMessage`` with ``chat_id`` + plain ``text``.

    No ``parse_mode``, so the text goes out verbatim and needs no MarkdownV2 escaping.
    The token sits in the URL path, so it is never logged or put in an error.
    """

    def __init__(self, token: str, chat_id: str, timeout: float = 10.0, **kw):
        token, chat_id = (token or "").strip(), str(chat_id or "").strip()
        if not token or not chat_id:
            raise ValueError("telegram sink needs both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        url = f"https://{TELEGRAM_HOST}/bot{token}/sendMessage"
        if not TELEGRAM_SEND_PATH.match(urllib.parse.urlsplit(url).path):
            raise ValueError("TELEGRAM_BOT_TOKEN has an unexpected format (want <digits>:<key> from @BotFather)")
        super().__init__(url, "telegram", timeout, **kw)
        self._token = token
        self.chat_id = chat_id

    @property
    def name(self) -> str:
        return "telegram"

    def __repr__(self) -> str:
        return f"TelegramSink(chat_id={self.chat_id!r}, token=***)"

    def _clean(self, text: str) -> str:
        return super()._clean(str(text).replace(self._token, "***"))

    def payload(self, alert: Alert) -> dict[str, Any]:
        return {"chat_id": self.chat_id, "text": alert.text()[:TELEGRAM_MAX_CHARS], "disable_web_page_preview": True}

    def _ok(self, raw: bytes) -> str | None:
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return None if data.get("ok") is True else f"telegram replied ok=false: {data.get('description', 'no description')}"


class AlertManager:
    def __init__(self, sinks: list, ledger=None, cooldown_seconds: int = 1800):
        self.sinks = sinks
        self.ledger = ledger
        self.cooldown = cooldown_seconds
        self._mem: dict[str, Any] = {}
        self.sent: list[Alert] = []

    def emit(self, alert: Alert) -> bool:
        now = utc_now()
        if self.ledger is not None:
            if not self.ledger.alert_due(alert.key, self.cooldown, now):
                return False
            self.ledger.alert_mark(alert.key, {"title": alert.title, **alert.data}, now)
        else:
            last = self._mem.get(alert.key)
            if last and (now - last).total_seconds() < self.cooldown:
                return False
            self._mem[alert.key] = now
        for s in self.sinks:
            try:
                s.send(alert)
            except Exception as exc:  # noqa: BLE001 - a broken sink never stops the loop
                log.warning("alert sink %s failed: %s", type(s).__name__,
                            redact(exc) if isinstance(exc, AlertSendError) else type(exc).__name__)
        self.sent.append(alert)
        return True


def build_sinks(settings, strict: bool = False) -> list:
    """Sinks from settings. Webhook and Telegram also turn on by themselves when their
    secrets are set (Telegram needs both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID), even
    if ALERT_SINKS doesn't list them. A misconfigured remote sink is logged and skipped
    so the 24/7 loop keeps running; ``strict=True`` (``test-alert``) raises instead."""
    names = [n.lower() for n in settings.alert_sinks]
    sinks: list = []
    if "stdout" in names:
        sinks.append(StdoutSink())
    if "file" in names:
        sinks.append(FileSink(settings.alert_file))
    remote = []
    if settings.alert_webhook_url:
        remote.append(("webhook", lambda: WebhookSink(settings.alert_webhook_url, settings.alert_webhook_kind)))
    if settings.telegram_bot_token and settings.telegram_chat_id:
        remote.append(("telegram", lambda: TelegramSink(settings.telegram_bot_token, settings.telegram_chat_id)))
    elif "telegram" in names:
        log.warning("ALERT_SINKS lists telegram but TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not both set")
    for name, make in remote:
        try:
            sinks.append(make())
        except ValueError as exc:
            if strict:
                raise ValueError(f"{name} sink misconfigured: {redact(exc)}") from None
            log.error("alert sink %s disabled: %s", name, redact(exc))
    return sinks


def sink_name(sink) -> str:
    return getattr(sink, "name", None) or {"StdoutSink": "stdout", "FileSink": "file"}.get(type(sink).__name__, type(sink).__name__)


def send_test_alert(sinks: list, text: str) -> dict[str, str | None]:
    """Push one test alert through every sink (no dedupe). {sink: None if ok, else redacted error}."""
    alert = Alert("system", "test-alert", text, data={"test": True})
    results: dict[str, str | None] = {}
    for s in sinks:
        try:
            s.send(alert)
            results[sink_name(s)] = None
        except Exception as exc:  # noqa: BLE001 - report every sink, never the secret
            results[sink_name(s)] = redact(exc) if isinstance(exc, AlertSendError) else type(exc).__name__
    return results


def make_alerts(settings, ledger=None) -> AlertManager:
    return AlertManager(build_sinks(settings), ledger, settings.alert_cooldown_seconds)
