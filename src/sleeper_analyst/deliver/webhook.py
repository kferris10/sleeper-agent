"""Slack / Discord incoming-webhook delivery.

The webhook URL is a secret; it comes from the WEBHOOK_URL environment
variable, never from config.toml. Long packets are split into chunks under
each platform's message limit, on line boundaries.
"""

from __future__ import annotations

import logging
import os

import httpx

from sleeper_analyst.deliver.base import Deliverer, DeliveryError

logger = logging.getLogger(__name__)

# Hard limits: Discord 2000 chars/message, Slack ~4000 chars of text.
CHUNK_LIMITS = {"discord": 1900, "slack": 3800}


def chunk_lines(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        line = line[:limit]  # a single pathological line still fits
        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


class WebhookDeliverer(Deliverer):
    def __init__(
        self,
        channel: str,
        url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        assert channel in ("slack", "discord")
        self.channel = channel
        self.url = url if url is not None else os.environ.get("WEBHOOK_URL", "")
        self.transport = transport

    def send(self, subject: str, markdown: str, html: str) -> None:
        if not self.url:
            raise DeliveryError(
                "WEBHOOK_URL is not set. Create an incoming webhook in your "
                f"{self.channel.title()} workspace and put its URL in .env."
            )
        body = f"**{subject}**\n\n{markdown}" if self.channel == "discord" else f"*{subject}*\n\n{markdown}"
        key = "content" if self.channel == "discord" else "text"
        with httpx.Client(transport=self.transport, timeout=30) as client:
            for chunk in chunk_lines(body, CHUNK_LIMITS[self.channel]):
                resp = client.post(self.url, json={key: chunk})
                if resp.status_code >= 400:
                    raise DeliveryError(
                        f"{self.channel} webhook returned {resp.status_code}: {resp.text[:200]}"
                    )
        logger.info("posted %r to %s webhook", subject, self.channel)
