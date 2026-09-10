"""Delivery channel abstraction: email (SMTP) or Slack/Discord webhook."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from sleeper_analyst.config import Settings

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """The packet could not be sent (bad config, auth failure, HTTP error)."""


class Deliverer(ABC):
    channel: str

    @abstractmethod
    def send(self, subject: str, markdown: str, html: str) -> None:
        """Send one packet. Raise DeliveryError on failure."""


class NullDeliverer(Deliverer):
    """channel = "none": packet files are written but nothing is sent."""

    channel = "none"

    def send(self, subject: str, markdown: str, html: str) -> None:
        logger.info("delivery channel is 'none'; skipping send of %r", subject)


def get_deliverer(settings: Settings) -> Deliverer:
    channel = settings.delivery.channel
    if channel == "email":
        from sleeper_analyst.deliver.email import EmailDeliverer

        return EmailDeliverer(settings.delivery)
    if channel in ("slack", "discord"):
        from sleeper_analyst.deliver.webhook import WebhookDeliverer

        return WebhookDeliverer(channel)
    if channel in ("none", ""):
        return NullDeliverer()
    raise DeliveryError(
        f"unknown delivery channel {channel!r} (expected email, slack, discord, or none)"
    )
