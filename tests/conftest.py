"""Test environment: isolated SQLite file, short dispatch window, slow
scheduler tick so tests drive escalation deterministically."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="jr-tests-"))
os.environ["JR_DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["JR_CONFIRM_WINDOW"] = "60"
os.environ["JR_MAX_ATTEMPTS"] = "2"
os.environ["JR_TICK"] = "3600"
os.environ["JR_SECRET_KEY"] = "test-secret"

import pytest  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import BackupContact, ContactKind, Region  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.create_all(engine)
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def region(db: Session) -> Region:
    r = Region(state="Test State", district="Test District", block="Test Block",
               emergency_number="108", confirm_window_sec=60)
    db.add(r)
    db.flush()
    db.add_all([
        BackupContact(region_id=r.id, name="ASHA One", kind=ContactKind.asha.value,
                      phone="9000000001", priority=10),
        BackupContact(region_id=r.id, name="PHC Two", kind=ContactKind.phc.value,
                      phone="9000000002", priority=20),
        BackupContact(region_id=r.id, name="Transport Three",
                      kind=ContactKind.local_transport.value,
                      phone="9000000003", priority=30),
        BackupContact(region_id=r.id, name="District Control",
                      kind=ContactKind.district_control.value,
                      phone="9000000009", priority=90),
    ])
    db.commit()
    db.refresh(r)
    return r
