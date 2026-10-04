"""CSRF double-submit protection on cookie-authenticated staff forms."""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import Role, User
from app.security import hash_password


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


def _make_user(db, username, password="pw123456"):
    pw, salt = hash_password(password)
    user = User(username=username, full_name=username.title(),
                role=Role.asha.value, password_hash=pw, salt=salt)
    db.add(user)
    db.commit()
    return user


class TestCsrf:
    def test_login_post_without_token_rejected(self, client, db):
        r = client.post("/login", data={"username": "x", "password": "y"})
        assert r.status_code == 403

    def test_login_post_with_wrong_token_rejected(self, client, db):
        client.get("/login")  # issues the jr_csrf cookie
        assert client.cookies.get("jr_csrf")
        r = client.post("/login", data={"username": "x", "password": "y",
                                        "csrf_token": "forged"})
        assert r.status_code == 403

    def test_login_post_with_matching_token_passes(self, client, db):
        _make_user(db, "csrfu")
        client.get("/login")
        token = client.cookies.get("jr_csrf")
        r = client.post("/login", data={
            "username": "csrfu", "password": "pw123456",
            "next": "/staff", "csrf_token": token,
        }, follow_redirects=False)
        # 303 to /staff = credentials accepted; a CSRF failure would be 403
        # and a credential failure would redirect with ?error=1.
        assert r.status_code == 303
        assert r.headers["location"] == "/staff"

    def test_login_page_carries_token_in_form(self, client, db):
        r = client.get("/login")
        assert r.status_code == 200
        assert 'name="csrf_token"' in r.text
        token = client.cookies.get("jr_csrf")
        assert token and token in r.text

    def test_protection_can_be_disabled(self, client, db, monkeypatch):
        monkeypatch.setattr(settings, "CSRF_ENABLED", False)
        r = client.post("/login", data={"username": "x", "password": "y"},
                        follow_redirects=False)
        # Reaches the credential check (bad credentials -> redirect), not 403.
        assert r.status_code == 303 and "error=1" in r.headers["location"]
