import pytest
from fastapi.testclient import TestClient

from brandwise_system.app import create_app
from brandwise_system.config import load
from brandwise_system.modules import Module, Row, Tile
from brandwise_system.modules.fakturownia import Fakturownia

ORIGIN = "https://panel.brandwise.pl"


class FakeMarek(Module):
    key, title = "marek", "Marek"

    def __init__(self, fail=False):
        self.fail = fail

    async def summary(self):
        if self.fail:
            raise ConnectionError("brak połączenia")
        return Tile(key="marek", title="Marek", stats=[Row("czeka na Ciebie", "2", tone="alert")],
                    rows_title="Czeka na Ciebie", rows=[Row("Strona Glina", "wycena wysłana")])


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "config.toml").write_text('''
host = "panel.brandwise.pl"
users = ["lukasz@brandwise.pl"]
[fakturownia]
domain = "brandwise"
kinds = ["vat"]
[[links."Strony firmowe"]]
name = "brandwise.pl"
url = "https://brandwise.pl/"
''')
    return load(tmp_path / "config.toml", env={"FAKTUROWNIA_API_TOKEN": "tok"})


@pytest.fixture
def make_client(cfg):
    def make(fk=None, marek=None):
        app = create_app(cfg, fakturownia=fk, marek=marek or FakeMarek())
        c = TestClient(app, base_url=ORIGIN, follow_redirects=False)
        return c, app
    return make


def login(client, app, email="lukasz@brandwise.pl"):
    db = app.state.db
    u = db.ensure_user(email)
    token = db.new_session(u["id"], 30)
    client.cookies.set("bw_system", token)
    return u
