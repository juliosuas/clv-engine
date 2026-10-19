"""Alert sinks with dedupe/cooldown (pattern from polymarket-desk api/alerts.py, MIT).

Sinks: stdout (logging), file (JSONL), webhook (Slack / Discord / generic JSON),
telegram (Bot API). Webhook URLs and tokens come from env only. These are the only
outbound POSTs the engine makes, and only to destinations you configure.
"""

from __future__ import annotations

import json
import logging
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


class WebhookSink:
    def __init__(self, url: str, kind: str = "slack", timeout: float = 8.0):
        self.url = url
        self.kind = kind
        self.timeout = timeout

    def payload(self, alert: Alert) -> dict[str, Any]:
        if self.kind == "discord":
            return {"content": alert.text()[:1900]}
        if self.kind == "generic":
            return {"kind": alert.kind, "key": alert.key, "title": alert.title, "body": alert.body, "data": alert.data}
        return {"text": alert.text()}

    def send(self, alert: Alert) -> None:
        body = json.dumps(self.payload(alert), default=str).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except Exception as exc:  # noqa: BLE001 - never crash the loop on an alert
            log.warning("webhook alert failed: %s", type(exc).__name__)


class TelegramSink(WebhookSink):
    def __init__(self, token: str, chat_id: str):
        super().__init__(f"https://api.telegram.org/bot{token}/sendMessage", "telegram")
        self.chat_id = chat_id

    def payload(self, alert: Alert) -> dict[str, Any]:
        return {"chat_id": self.chat_id, "text": alert.text()[:4000], "disable_web_page_preview": True}


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
            except Exception as exc:  # noqa: BLE001
                log.warning("alert sink %s failed: %s", type(s).__name__, exc)
        self.sent.append(alert)
        return True


def make_alerts(settings, ledger=None) -> AlertManager:
    sinks: list = []
    for name in settings.alert_sinks:
        name = name.lower()
        if name == "stdout":
            sinks.append(StdoutSink())
        elif name == "file":
            sinks.append(FileSink(settings.alert_file))
        elif name == "webhook" and settings.alert_webhook_url:
            sinks.append(WebhookSink(settings.alert_webhook_url, settings.alert_webhook_kind))
        elif name == "telegram" and settings.telegram_bot_token and settings.telegram_chat_id:
            sinks.append(TelegramSink(settings.telegram_bot_token, settings.telegram_chat_id))
    if settings.alert_webhook_url and "webhook" not in [n.lower() for n in settings.alert_sinks]:
        sinks.append(WebhookSink(settings.alert_webhook_url, settings.alert_webhook_kind))
    return AlertManager(sinks, ledger, settings.alert_cooldown_seconds)
