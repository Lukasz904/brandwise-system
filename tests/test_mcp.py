import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

from .conftest import ORIGIN, login

CB = "https://claude.ai/api/mcp/auth_callback"


def pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def connect(c, app):
    """Pełny przepływ jak w claude.ai: 401 → metadane → DCR → zgoda → kod → tokeny."""
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.status_code == 401 and "resource_metadata=" in r.headers["www-authenticate"]
    prm = c.get("/.well-known/oauth-protected-resource").json()
    assert prm["resource"] == ORIGIN + "/mcp"
    asm = c.get("/.well-known/oauth-authorization-server").json()
    assert asm["code_challenge_methods_supported"] == ["S256"]
    reg = c.post("/oauth/rejestracja", json={"client_name": "Claude", "redirect_uris": [CB]})
    assert reg.status_code == 201
    cid = reg.json()["client_id"]
    v, ch = pkce()
    q = {"response_type": "code", "client_id": cid, "redirect_uri": CB, "code_challenge": ch,
         "code_challenge_method": "S256", "state": "xyz", "resource": ORIGIN + "/mcp"}
    r = c.get("/oauth/autoryzuj", params=q)
    assert r.status_code == 303 and r.headers["location"].startswith("/logowanie?dalej=%2Foauth%2Fautoryzuj")
    login(c, app)
    r = c.get("/oauth/autoryzuj", params=q)
    assert r.status_code == 200 and "Zezwól" in r.text and "https://claude.ai" in r.headers["content-security-policy"]
    r = c.post("/oauth/autoryzuj", data={**q, "decyzja": "tak"}, headers={"Origin": ORIGIN})
    loc = urlparse(r.headers["location"])
    assert r.status_code == 303 and loc.netloc == "claude.ai"
    code = parse_qs(loc.query)["code"][0]
    assert parse_qs(loc.query)["state"] == ["xyz"]
    c.cookies.clear()
    tok = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CB,
                                       "client_id": cid, "code_verifier": v})
    assert tok.status_code == 200, tok.text
    # kod jednorazowy
    again = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CB,
                                         "client_id": cid, "code_verifier": v})
    assert again.json()["error"] == "invalid_grant"
    return cid, tok.json()


def rpc(c, at, method, params=None, id_=1):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
                  headers={"Authorization": f"Bearer {at}"})


def test_full_flow_and_tools(make_client):
    c, app = make_client()
    F = app.state.finance
    kid = F.add_client("LUX MED")
    F.save_order({"day": "2026-10-01", "title": "Kampania jesień", "client_id": kid, "payer_id": kid, "price": 100000})
    cid, t = connect(c, app)
    at = t["access_token"]
    init = rpc(c, at, "initialize", {"protocolVersion": "2025-06-18"}).json()
    assert init["result"]["protocolVersion"] == "2025-06-18"
    names = [x["name"] for x in rpc(c, at, "tools/list").json()["result"]["tools"]]
    assert names == ["lista_klientow", "szukaj", "dodaj_prospekt"]
    assert "LUX MED" in rpc(c, at, "tools/call", {"name": "lista_klientow"}).json()["result"]["content"][0]["text"]
    s = rpc(c, at, "tools/call", {"name": "szukaj", "arguments": {"fraza": "kampania"}}).json()["result"]
    assert "Kampania jesień" in s["content"][0]["text"] and "zlecenie" in s["content"][0]["text"]
    # nieznany klient bez zgody na nowego – błąd, nic nie powstaje
    r = rpc(c, at, "tools/call", {"name": "dodaj_prospekt", "arguments": {"tytul": "X", "klient": "Nowa Firma"}}).json()
    assert r["result"]["isError"] and F.find_client("Nowa Firma") is None
    r = rpc(c, at, "tools/call", {"name": "dodaj_prospekt", "arguments": {
        "tytul": "Strona kariery", "klient": "luxmed", "kwota_netto": 24500.5, "notatka": "wątek z 10.10"}}).json()
    assert not r["result"]["isError"], r
    from brandwise_system.finance import Filters
    p = F.orders(Filters(kind="prospekt"))[0]
    assert p["title"] == "Strona kariery" and p["client_id"] == kid and p["total"] == 2450050
    assert p["note"].startswith("[Asystent]")
    assert F.totals(Filters())["n"] == 1                       # zlecenia bez zmian
    r = rpc(c, at, "tools/call", {"name": "dodaj_prospekt", "arguments": {
        "tytul": "Logo", "klient": "Nowa Firma", "nowy_klient": True}}).json()
    assert not r["result"]["isError"] and F.find_client("Nowa Firma")
    # odświeżenie z rotacją; ponowne użycie starego tokenu odcina połączenie
    r1 = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid})
    assert r1.status_code == 200
    reuse = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid})
    assert reuse.json()["error"] == "invalid_grant"
    assert rpc(c, at, "ping").status_code == 401 and rpc(c, r1.json()["access_token"], "ping").status_code == 401


def test_rejects_foreign_redirects_and_bad_pkce(make_client):
    c, app = make_client()
    bad = c.post("/oauth/rejestracja", json={"redirect_uris": ["https://evil.example/cb"]})
    assert bad.status_code == 400
    cid = c.post("/oauth/rejestracja", json={"redirect_uris": [CB]}).json()["client_id"]
    login(c, app)
    v, ch = pkce()
    q = {"response_type": "code", "client_id": cid, "redirect_uri": "https://evil.example/cb", "code_challenge": ch,
         "code_challenge_method": "S256"}
    assert c.get("/oauth/autoryzuj", params=q).status_code == 400
    q["redirect_uri"] = CB
    q["code_challenge_method"] = "plain"
    assert c.get("/oauth/autoryzuj", params=q).status_code == 400
    q["code_challenge_method"] = "S256"
    # zgoda bez nagłówka Origin (CSRF) – odrzucona
    assert c.post("/oauth/autoryzuj", data={**q, "decyzja": "tak"}).status_code == 403
    r = c.post("/oauth/autoryzuj", data={**q, "decyzja": "tak"}, headers={"Origin": ORIGIN})
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    wrong = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CB,
                                         "client_id": cid, "code_verifier": "x" * 50})
    assert wrong.json()["error"] == "invalid_grant"
    # odmowa
    r = c.post("/oauth/autoryzuj", data={**q, "decyzja": "nie"}, headers={"Origin": ORIGIN})
    assert "error=access_denied" in r.headers["location"]


def test_revoke_in_keys_page(make_client):
    c, app = make_client()
    cid, t = connect(c, app)
    login(c, app)
    page = c.get("/klucze").text
    assert "Połączone aplikacje" in page and "Claude" in page
    gid = app.state.db.one("SELECT id FROM oauth_grants")["id"]
    c.post(f"/klucze/aplikacje/{gid}/odlacz", headers={"Origin": ORIGIN})
    assert rpc(c, t["access_token"], "ping").status_code == 401
    assert c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                        "client_id": cid}).json()["error"] == "invalid_grant"


def test_login_returns_to_next(make_client):
    c, app = make_client()
    login(c, app)
    assert c.get("/logowanie?dalej=/klucze").headers["location"] == "/klucze"
    assert c.get("/logowanie?dalej=//evil.example").headers["location"] == "/"
