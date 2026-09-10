import httpx
import pytest

from sleeper_analyst.config import DeliveryConfig, Settings
from sleeper_analyst.deliver.base import DeliveryError, NullDeliverer, get_deliverer
from sleeper_analyst.deliver.email import EmailDeliverer
from sleeper_analyst.deliver.webhook import CHUNK_LIMITS, WebhookDeliverer, chunk_lines
from sleeper_analyst.store import Store


# -- email -------------------------------------------------------------------


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls: list[tuple] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append(("send", msg))


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    FakeSMTP.instances = []


def make_email_deliverer(**overrides) -> EmailDeliverer:
    cfg = DeliveryConfig(channel="email", email_to="owner@example.com", **overrides)
    return EmailDeliverer(cfg, password="app-password", smtp_factory=FakeSMTP)


def test_email_sends_multipart_message():
    d = make_email_deliverer()
    d.send("Week 2 packet", "# md body", "<div>html body</div>")

    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp.gmail.com" and smtp.port == 587
    assert ("starttls",) in smtp.calls
    assert ("login", "owner@example.com", "app-password") in smtp.calls
    msg = next(c[1] for c in smtp.calls if c[0] == "send")
    assert msg["Subject"] == "Week 2 packet"
    assert msg["To"] == "owner@example.com"
    parts = {p.get_content_type() for p in msg.walk()}
    assert "text/plain" in parts and "text/html" in parts


def test_email_requires_password():
    cfg = DeliveryConfig(channel="email", email_to="owner@example.com")
    d = EmailDeliverer(cfg, password="", smtp_factory=FakeSMTP)
    with pytest.raises(DeliveryError, match="SMTP_PASSWORD"):
        d.send("s", "md", "html")
    assert not FakeSMTP.instances  # never opened a connection


def test_email_requires_recipient():
    cfg = DeliveryConfig(channel="email", email_to="")
    d = EmailDeliverer(cfg, password="pw", smtp_factory=FakeSMTP)
    with pytest.raises(DeliveryError, match="email_to"):
        d.send("s", "md", "html")


# -- webhook -----------------------------------------------------------------


def test_chunk_lines_respects_limit_and_boundaries():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(100))
    chunks = chunk_lines(text, 500)
    assert all(len(c) <= 500 for c in chunks)
    assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_discord_webhook_chunks_long_packet():
    received: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        received.append(json.loads(request.content))
        return httpx.Response(204)

    d = WebhookDeliverer(
        "discord", url="https://discord.test/webhook", transport=httpx.MockTransport(handler)
    )
    long_md = "\n".join(f"row {i} " + "y" * 90 for i in range(60))
    d.send("Week 2 packet", long_md, "<html ignored>")

    assert len(received) > 1
    assert all(len(p["content"]) <= CHUNK_LIMITS["discord"] for p in received)
    assert received[0]["content"].startswith("**Week 2 packet**")


def test_webhook_http_error_raises():
    d = WebhookDeliverer(
        "slack",
        url="https://slack.test/webhook",
        transport=httpx.MockTransport(lambda r: httpx.Response(403, text="no_service")),
    )
    with pytest.raises(DeliveryError, match="403"):
        d.send("s", "md", "html")


def test_webhook_requires_url():
    d = WebhookDeliverer("discord", url="")
    with pytest.raises(DeliveryError, match="WEBHOOK_URL"):
        d.send("s", "md", "html")


# -- routing + idempotency ---------------------------------------------------


def test_get_deliverer_routes_by_channel():
    assert get_deliverer(Settings(delivery=DeliveryConfig(channel="email"))).channel == "email"
    assert get_deliverer(Settings(delivery=DeliveryConfig(channel="discord"))).channel == "discord"
    assert isinstance(get_deliverer(Settings(delivery=DeliveryConfig(channel="none"))), NullDeliverer)
    with pytest.raises(DeliveryError, match="unknown delivery channel"):
        get_deliverer(Settings(delivery=DeliveryConfig(channel="carrier-pigeon")))


def test_store_tracks_deliveries(tmp_path):
    with Store(tmp_path / "history.db") as store:
        assert not store.was_delivered("2026", 2)
        store.mark_delivered("2026", 2, "email")
        assert store.was_delivered("2026", 2)
        assert not store.was_delivered("2026", 3)
