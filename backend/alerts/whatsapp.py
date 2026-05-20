"""
WhatsApp alert sender.

Supports three providers (selected in config.yaml):
  twilio  — Twilio WhatsApp API
  meta    — Meta (WhatsApp) Cloud API
  webhook — generic HTTP POST (e.g. to your own gateway / n8n / CallMeBot)
  disabled — alerts are logged only

All network calls run in a background thread so the detection
pipeline never blocks on a slow HTTP request.
"""
from __future__ import annotations

import threading

import requests

from backend.core.config import CONFIG


class WhatsAppNotifier:
    def __init__(self):
        wa = CONFIG.get_path("alerts.whatsapp", {}) or {}
        self.provider = str(wa.get("provider", "disabled")).lower()
        self.cfg = wa

    # ── public API ───────────────────────────────────────────────────
    def send(self, message: str, image_url: str | None = None) -> None:
        """Fire-and-forget alert (non-blocking)."""
        threading.Thread(
            target=self._send_blocking, args=(message, image_url), daemon=True
        ).start()

    # ── implementation ───────────────────────────────────────────────
    def _send_blocking(self, message: str, image_url: str | None) -> None:
        try:
            if self.provider == "twilio":
                self._twilio(message, image_url)
            elif self.provider == "meta":
                self._meta(message, image_url)
            elif self.provider == "webhook":
                self._webhook(message, image_url)
            else:
                print(f"[whatsapp] (disabled) {message}")
        except Exception as exc:
            print(f"[whatsapp] send failed: {exc}")

    def _twilio(self, message: str, image_url: str | None) -> None:
        sid = self.cfg.get("twilio_sid")
        token = self.cfg.get("twilio_token")
        if not (sid and token):
            print("[whatsapp] twilio credentials missing — logged only:", message)
            return
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        data = {
            "From": self.cfg.get("twilio_from"),
            "To": self.cfg.get("twilio_to"),
            "Body": message,
        }
        if image_url:
            data["MediaUrl"] = image_url
        resp = requests.post(url, data=data, auth=(sid, token), timeout=15)
        resp.raise_for_status()
        print("[whatsapp] twilio alert sent.")

    def _meta(self, message: str, image_url: str | None) -> None:
        token = self.cfg.get("meta_token")
        phone_id = self.cfg.get("meta_phone_id")
        to = self.cfg.get("meta_to")
        if not (token and phone_id and to):
            print("[whatsapp] meta credentials missing — logged only:", message)
            return
        url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
        headers = {"Authorization": f"Bearer {token}"}
        if image_url:
            payload = {
                "messaging_product": "whatsapp", "to": to, "type": "image",
                "image": {"link": image_url, "caption": message},
            }
        else:
            payload = {
                "messaging_product": "whatsapp", "to": to, "type": "text",
                "text": {"body": message},
            }
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        print("[whatsapp] meta alert sent.")

    def _webhook(self, message: str, image_url: str | None) -> None:
        url = self.cfg.get("webhook_url")
        if not url:
            print("[whatsapp] webhook_url missing — logged only:", message)
            return
        resp = requests.post(
            url, json={"message": message, "image_url": image_url}, timeout=15
        )
        resp.raise_for_status()
        print("[whatsapp] webhook alert sent.")


# Singleton
NOTIFIER = WhatsAppNotifier()
