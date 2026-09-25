"""WhatsApp SLA reminders stop after 2 per ticket per stage."""
from app.config import Settings


def test_reminder_cap_defaults_to_two(monkeypatch):
    monkeypatch.delenv("REMINDER_CAP", raising=False)
    assert Settings(_env_file=None).reminder_cap == 2
