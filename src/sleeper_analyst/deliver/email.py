"""SMTP email delivery (built for Gmail: STARTTLS + app password).

The app password comes from the SMTP_PASSWORD environment variable (via .env
locally, repository secret in GitHub Actions) — never from config.toml.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable

from sleeper_analyst.config import DeliveryConfig
from sleeper_analyst.deliver.base import Deliverer, DeliveryError

logger = logging.getLogger(__name__)


class EmailDeliverer(Deliverer):
    channel = "email"

    def __init__(
        self,
        cfg: DeliveryConfig,
        password: str | None = None,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
    ) -> None:
        self.cfg = cfg
        self.password = password if password is not None else os.environ.get("SMTP_PASSWORD", "")
        self.smtp_factory = smtp_factory or smtplib.SMTP

    def build_message(self, subject: str, markdown: str, html: str) -> EmailMessage:
        user = self.cfg.smtp_user or self.cfg.email_to
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.cfg.email_from or user
        msg["To"] = self.cfg.email_to
        msg.set_content(markdown)  # plain-text fallback is the markdown itself
        msg.add_alternative(html, subtype="html")
        return msg

    def send(self, subject: str, markdown: str, html: str) -> None:
        if not self.cfg.email_to:
            raise DeliveryError("[delivery] email_to is not set in config.toml")
        if not self.password:
            raise DeliveryError(
                "SMTP_PASSWORD is not set. For Gmail, create an app password at "
                "https://myaccount.google.com/apppasswords and put it in .env "
                "(or the SMTP_PASSWORD repo secret for GitHub Actions)."
            )
        user = self.cfg.smtp_user or self.cfg.email_to
        msg = self.build_message(subject, markdown, html)
        try:
            with self.smtp_factory(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(user, self.password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(f"SMTP send failed: {exc}") from exc
        logger.info("emailed %r to %s", subject, self.cfg.email_to)
