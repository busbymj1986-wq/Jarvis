import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("JARVIS_API_KEY", "secret")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("SIP_DOMAIN", "jarvis.sip.twilio.com")
    import app as app_module
    from jarvis import store
    store.init_db()
    app_module.app.testing = True
    return app_module.app.test_client()


AUTH = {"Authorization": "Bearer secret"}
