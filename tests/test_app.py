from datetime import date, timedelta

import httpx

from brandwise_system.modules.fakturownia import Fakturownia

from .conftest import ORIGIN, FakeMarek, login


def fk_with(invoices, calls=None):
    def h(request):
        if calls is not None:
            calls.append(request)
        if request.url.path.endswith("change_status.json"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json=invoices if request.url.params["page"] == "1" else [])
    return Fakturownia("brandwise", "tok", ["vat"], transport=httpx.MockTransport(h))


def one(i, days, gross="1230.00"):
    return {"id": i, "number": f"FV {i}/2026", "buyer_name": f"Klient {i}", "kind": "vat", "status": "issued",
            "payment_to": (date.today() + timedelta(days=days)).isoformat(), "price_gross": gross,
            "price_net": "1000", "paid": "0", "currency": "PLN"}


def test_requires_login(make_client):
    c, app = make_client()
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/logowanie"
    assert c.get("/logowanie").status_code == 200
    assert c.get("/faktury").status_code == 303
    assert c.post("/faktury/1/oplacona", headers={"Origin": ORIGIN}).status_code == 401


def test_post_requires_origin(make_client):
    c, app = make_client()
    login(c, app)
    assert c.post("/faktury/1/oplacona").status_code == 403
    assert c.post("/faktury/1/oplacona", headers={"Origin": "https://zly.example"}).status_code == 403


def test_dashboard_tiles_and_failure_isolated(make_client):
    c, app = make_client(fk=fk_with([one(1, -5), one(2, 3)]), marek=FakeMarek(fail=True))
    login(c, app)
    r = c.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Brak danych: brak połączenia" in html            # Marek padł, reszta działa
    assert "Klient 1" in html and "5 dni po terminie" in html
    assert "2 460,00 zł" in html                              # suma nieopłaconych
    assert "Finanse ·" in html and "do zafakturowania" in html
    assert 'href="https://brandwise.pl/"' in html
    assert r.headers["content-security-policy"].startswith("default-src 'none'")


def test_invoices_page_and_mark_paid(make_client):
    calls = []
    c, app = make_client(fk=fk_with([one(7, -1)], calls))
    login(c, app)
    r = c.get("/faktury")
    assert r.status_code == 200 and "FV 7/2026" in r.text and "1 dni po terminie" in r.text
    r = c.post("/faktury/7/oplacona", headers={"Origin": ORIGIN})
    assert r.status_code == 303 and r.headers["location"] == "/faktury?ok=1"
    assert calls[-1].url.path == "/invoices/7/change_status.json"
    assert app.state.db.one("SELECT event FROM audit ORDER BY id DESC LIMIT 1")["event"] == "invoice_marked_paid"


def test_fakturownia_error_shown(make_client):
    c, app = make_client(fk=Fakturownia("", "", ["vat"]))
    login(c, app)
    assert "brak konfiguracji" in c.get("/faktury").text
    assert "brak konfiguracji" in c.get("/").text


def test_registration_needs_valid_invite(make_client):
    c, app = make_client()
    h = {"Origin": ORIGIN}
    assert c.post("/auth/rejestracja/opcje", json={"zaproszenie": "zle"}, headers=h).status_code == 403
    token = app.state.db.create_invite("lukasz@brandwise.pl")
    r = c.post("/auth/rejestracja/opcje", json={"zaproszenie": token}, headers=h)
    assert r.status_code == 200
    o = r.json()
    assert o["rp"]["id"] == "panel.brandwise.pl" and o["user"]["name"] == "lukasz@brandwise.pl"
    assert o["authenticatorSelection"]["residentKey"] == "required"
    assert o["authenticatorSelection"]["userVerification"] == "required"
    # fałszywa odpowiedź uwierzytelniacza – odrzucona, bez sesji
    r = c.post("/auth/rejestracja", json={"zaproszenie": token, "credential": {"id": "x"}}, headers=h)
    assert r.status_code == 400 and "bw_system" not in r.cookies
    assert c.get("/zaproszenie/" + token).status_code == 200


def test_invite_only_for_listed_users(make_client):
    c, app = make_client()
    token = app.state.db.create_invite("obcy@example.com")
    r = c.post("/auth/rejestracja/opcje", json={"zaproszenie": token}, headers={"Origin": ORIGIN})
    assert r.status_code == 403


def test_login_options_and_unknown_credential(make_client):
    c, app = make_client()
    h = {"Origin": ORIGIN}
    r = c.post("/auth/logowanie/opcje", headers=h)
    assert r.status_code == 200 and r.json()["rpId"] == "panel.brandwise.pl"
    r = c.post("/auth/logowanie", json={"credential": {"id": "AAAA", "rawId": "AAAA"}}, headers=h)
    assert r.status_code == 403


def test_cannot_delete_last_key(make_client):
    c, app = make_client()
    u = login(c, app)
    app.state.db.add_credential(u["id"], b"\x01\x02", b"pk", 0, "MacBook")
    r = c.post("/klucze/AQI/usun", headers={"Origin": ORIGIN})
    assert r.status_code == 400
    assert len(app.state.db.credentials_of(u["id"])) == 1
