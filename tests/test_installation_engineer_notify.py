"""Engineer WhatsApp/push alert when an installation is assigned to them."""
import json
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.main import app as _app  # noqa: F401 — registers every model's mappers
from app.models.installation import Installation, InstallationStatus
from app.models.user import User
from app.services import installation_notify, installation_workflow


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add_all([
            User(id=1, username="mgr", name="Meera", role="MANAGER", phone="+919000000001",
                 password_hash="x", active=True),
            User(id=2, username="eng", name="Ravi", role="ENGINEER", phone="+919000000002",
                 password_hash="x", active=True),
        ])
        db.add(Installation(
            id=10, reference="INS-2026-00010", business_name="Cafe Blue",
            business_category="Restaurant", contact_name="Asha", phone="9000000003",
            invoice_number="INV-1", city="Mysuru",
            expected_installation_date=date(2026, 10, 12),
            status=InstallationStatus.NEW.value,
        ))
        db.commit()
    yield Session


@pytest.fixture
def notified(monkeypatch):
    calls = []
    monkeypatch.setattr(
        installation_notify, "notify_engineer_assigned",
        lambda inst_id, eng_id: calls.append((inst_id, eng_id)),
    )
    return calls


def _assign(Session, actor_id, engineer_id):
    with Session() as db:
        inst = db.get(Installation, 10)
        actor = db.get(User, actor_id)
        installation_workflow.assign(db, inst, actor, engineer_id)


def test_assigning_notifies_the_engineer(db_session, notified):
    _assign(db_session, actor_id=1, engineer_id=2)
    assert notified == [(10, 2)]


def test_reassigning_to_the_same_engineer_does_not_renotify(db_session, notified):
    _assign(db_session, actor_id=1, engineer_id=2)
    _assign(db_session, actor_id=1, engineer_id=2)
    assert notified == [(10, 2)]


def test_self_assign_does_not_notify(db_session, notified):
    _assign(db_session, actor_id=1, engineer_id=1)
    assert notified == []


def test_whatsapp_payload_uses_template_variables(db_session, notified, monkeypatch):
    _assign(db_session, actor_id=1, engineer_id=2)

    sent = []
    monkeypatch.setattr(installation_notify, "SessionLocal", db_session)
    monkeypatch.setattr(installation_notify, "_twilio_configured", lambda: True)
    monkeypatch.setattr(
        installation_notify, "_twilio_endpoint",
        lambda: ("https://twilio.test", ("sid", "tok"), "whatsapp:+910000000000"),
    )
    monkeypatch.setattr(
        installation_notify, "_send_one",
        lambda url, auth, data, who: sent.append(data) or True,
    )

    class _Settings:
        twilio_install_engineer_assign_content_sid = "HXtest"

    monkeypatch.setattr(installation_notify, "get_settings", lambda: _Settings())
    installation_notify._notify_engineer(10, 2)

    assert len(sent) == 1
    assert sent[0]["To"] == "whatsapp:+919000000002"
    assert sent[0]["ContentSid"] == "HXtest"
    assert json.loads(sent[0]["ContentVariables"]) == {
        "1": "Ravi",
        "2": "INS-2026-00010",
        "3": "Cafe Blue, Mysuru",
        "4": "12 Oct 2026",
        "5": "Meera",
    }
