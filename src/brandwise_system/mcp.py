"""Connector dla Claude (claude.ai): serwer MCP + minimalny serwer OAuth 2.1.

Po co: Twój projekt „Asystent Brandwise” w Claude może dopisywać prospekty i szukać w zleceniach.

Bezpieczeństwo:
- Dostęp daje wyłącznie zalogowany człowiek (passkey) na ekranie zgody /oauth/autoryzuj.
- Przekierowanie po zgodzie tylko na adresy Claude z listy ALLOWED_REDIRECTS (rejestracja innych odrzucana) –
  nikt nie zbuduje własnej „aplikacji” i nie wyłudzi kodu.
- PKCE S256 obowiązkowe; kody jednorazowe (5 min); tokeny dostępu 1 h, odświeżające rotowane (60 dni);
  w bazie tylko skróty. Odłączenie: Klucze → „Połączone aplikacje”.
- Narzędzia tylko dopisują prospekty i czytają; nie ma edycji, usuwania, zleceń ani faktur.
  Treść maili czytana przez Asystenta może zawierać polecenia – dlatego najgorszy skutek to zbędny prospekt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import date, datetime, timedelta
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .db import iso, now, sha

ALLOWED_REDIRECTS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
SCOPE = "prospekty"
CODE_MIN, ACCESS_H, REFRESH_D = 5, 1, 60
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_CLIENTS = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    challenge TEXT NOT NULL,
    user_email TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_grants (           -- jedno „połączenie” = łańcuch tokenów
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL,
    user_email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    grant_id INTEGER NOT NULL REFERENCES oauth_grants(id),
    kind TEXT NOT NULL,                              -- access | refresh
    expires_at TEXT NOT NULL,
    used_at TEXT
);
"""


def init(db) -> None:
    db.conn.executescript(SCHEMA)


def b64url_sha256(v: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


# ---------- magazyn ----------
def register_client(db, name: str, redirect_uris: list[str]) -> str:
    cid = "bwc_" + secrets.token_urlsafe(16)
    db.q("INSERT INTO oauth_clients(client_id, name, redirect_uris, created_at) VALUES (?,?,?,?)",
         (cid, name[:80] or "Claude", json.dumps(redirect_uris), iso(now())))
    return cid


def client(db, cid: str):
    return db.one("SELECT * FROM oauth_clients WHERE client_id=?", (cid,))


def issue_tokens(db, grant_id: int) -> dict:
    access, refresh = "bwa_" + secrets.token_urlsafe(32), "bwr_" + secrets.token_urlsafe(32)
    db.q("INSERT INTO oauth_tokens(token_hash, grant_id, kind, expires_at) VALUES (?,?,?,?)",
         (sha(access), grant_id, "access", iso(now() + timedelta(hours=ACCESS_H))))
    db.q("INSERT INTO oauth_tokens(token_hash, grant_id, kind, expires_at) VALUES (?,?,?,?)",
         (sha(refresh), grant_id, "refresh", iso(now() + timedelta(days=REFRESH_D))))
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_H * 3600,
            "refresh_token": refresh, "scope": SCOPE}


def token_grant(db, token: str, kind: str):
    return db.one("SELECT t.*, g.user_email, g.client_id, g.revoked_at AS grant_revoked FROM oauth_tokens t "
                  "JOIN oauth_grants g ON g.id=t.grant_id WHERE t.token_hash=? AND t.kind=?", (sha(token), kind))


def grants(db):
    return db.q("SELECT g.*, c.name AS client_name FROM oauth_grants g JOIN oauth_clients c ON c.client_id=g.client_id "
                "WHERE g.revoked_at IS NULL ORDER BY g.created_at")


def revoke_grant(db, gid: int) -> None:
    db.q("UPDATE oauth_grants SET revoked_at=? WHERE id=?", (iso(now()), gid))


# ---------- narzędzia ----------
TOOLS = [
    {"name": "lista_klientow",
     "description": "Lista klientów w systemie Brandwise (nazwy dokładnie tak, jak trzeba je podać w dodaj_prospekt).",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "szukaj",
     "description": ("Szuka w zleceniach i prospektach po fragmencie tytułu i/lub kliencie. Użyj przed dodaniem "
                     "prospektu, żeby nie dublować spraw. Zwraca maks. 25 najnowszych wyników."),
     "inputSchema": {"type": "object", "properties": {
         "fraza": {"type": "string", "description": "fragment tytułu, np. 'kampania'"},
         "klient": {"type": "string", "description": "nazwa klienta (opcjonalnie)"},
         "rodzaj": {"type": "string", "enum": ["prospekt", "zlecenie", "oba"], "default": "oba"}},
         "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "dodaj_prospekt",
     "description": ("Dodaje prospekt – sprawę w zaawansowanych rozmowach, jeszcze niezaakceptowaną. Nie liczy się do "
                     "finansów; Łukasz sam przenosi go do zleceń. Kwota netto w złotych. Jeśli klienta nie ma na liście, "
                     "ustaw nowy_klient=true (tylko gdy masz pewność, że to nowy klient)."),
     "inputSchema": {"type": "object", "required": ["tytul", "klient"], "properties": {
         "tytul": {"type": "string", "maxLength": 200},
         "klient": {"type": "string", "maxLength": 120},
         "kwota_netto": {"type": "number", "minimum": 0, "maximum": 10000000},
         "data": {"type": "string", "description": "RRRR-MM-DD, domyślnie dziś"},
         "zleceniodawca": {"type": "string", "maxLength": 120, "description": "osoba po stronie klienta"},
         "notatka": {"type": "string", "maxLength": 1500, "description": "stan rozmów, ustalenia, link do wątku"},
         "nowy_klient": {"type": "boolean", "default": False}},
         "additionalProperties": False}},
]


class ToolError(Exception):
    pass


def _s(args: dict, k: str, n: int) -> str:
    v = args.get(k, "")
    if not isinstance(v, str):
        raise ToolError(f"Pole {k} musi być tekstem.")
    return v.strip()[:n]


def call_tool(app, name: str, args: dict, who: str) -> str:
    from .finance import Filters, to_grosze
    F = app.state.finance
    if name == "lista_klientow":
        return "\n".join(c["name"] for c in F.clients())
    if name == "szukaj":
        fraza, kl, rodzaj = _s(args, "fraza", 100), _s(args, "klient", 120), args.get("rodzaj", "oba")
        cids: tuple = ()
        if kl:
            c = F.find_client(kl)
            if c is None:
                return f"Nie ma klienta „{kl}”. Sprawdź lista_klientow."
            cids = (c["id"],)
        out = []
        for kind in (("prospekt", "zlecenie") if rodzaj not in ("prospekt", "zlecenie") else (rodzaj,)):
            for o in F.orders(Filters(q=fraza, client_id=cids, kind=kind), limit=25):
                out.append((o["day"], kind, o["title"], o["client_name"], o["total"], o["status"]))
        out.sort(reverse=True)
        if not out:
            return "Brak wyników."
        return "\n".join(f"{d} · {k} · {t} · {c} · {g / 100:.2f} zł netto · {s or '-'}" for d, k, t, c, g, s in out[:25])
    if name == "dodaj_prospekt":
        tytul, kl = _s(args, "tytul", 200), _s(args, "klient", 120)
        if not tytul or not kl:
            raise ToolError("Podaj tytuł i klienta.")
        day = _s(args, "data", 10) or date.today().isoformat()
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError as e:
            raise ToolError("Data w formacie RRRR-MM-DD.") from e
        kwota = args.get("kwota_netto", 0) or 0
        if not isinstance(kwota, (int, float)) or isinstance(kwota, bool) or not 0 <= kwota <= 10_000_000:
            raise ToolError("Kwota netto 0–10 000 000 zł.")
        c = F.find_client(kl)
        created = False
        if c is None:
            if args.get("nowy_klient") is not True:
                raise ToolError(f"Nie ma klienta „{kl}”. Użyj nazwy z lista_klientow albo ustaw nowy_klient=true.")
            cid, created = F.add_client(kl), True
        else:
            cid = c["id"]
        statuses = F.get_list("status")
        note = "[Asystent] " + _s(args, "notatka", 1500)
        oid = F.save_order({"kind": "prospekt", "day": day, "title": tytul, "client_id": cid, "payer_id": cid,
                            "principal": _s(args, "zleceniodawca", 120), "price": to_grosze(str(kwota).replace(".", ",")),
                            "qty": 1, "status": statuses[0] if statuses else "", "invoice_status": "Brak",
                            "note": note.strip()})
        app.state.db.log("mcp_prospect_added", who, f"{oid} {tytul[:80]}" + (" +klient" if created else ""))
        return (f"Dodano prospekt #{oid}: {tytul} ({kl}{', nowy klient' if created else ''}). "
                f"Podgląd: {app.state.cfg.origin}/prospekty/{oid}")
    raise ToolError(f"Nieznane narzędzie {name}.")


# ---------- HTTP ----------
router = APIRouter()


def _origin(request: Request) -> str:
    return request.app.state.cfg.origin


def _resource(request: Request) -> str:
    return _origin(request) + "/mcp"


def _oauth_err(code: str, desc: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code, "error_description": desc}, status_code=status,
                        headers={"Cache-Control": "no-store"})


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource(request: Request):
    return {"resource": _resource(request), "authorization_servers": [_origin(request)],
            "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"],
            "resource_name": "Brandwise – system firmowy"}


@router.get("/.well-known/oauth-authorization-server")
async def as_metadata(request: Request):
    o = _origin(request)
    return {"issuer": o, "authorization_endpoint": o + "/oauth/autoryzuj", "token_endpoint": o + "/oauth/token",
            "registration_endpoint": o + "/oauth/rejestracja", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE]}


@router.post("/oauth/rejestracja")
async def register(request: Request):
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _oauth_err("invalid_client_metadata", "JSON wymagany.")
    uris = body.get("redirect_uris") or []
    if not isinstance(uris, list) or not uris or any(u not in ALLOWED_REDIRECTS for u in uris):
        return _oauth_err("invalid_redirect_uri", "Dozwolone są wyłącznie adresy zwrotne Claude.")
    if db.one("SELECT COUNT(*) n FROM oauth_clients")["n"] >= MAX_CLIENTS:
        db.q("DELETE FROM oauth_clients WHERE client_id NOT IN (SELECT client_id FROM oauth_grants) "
             "AND client_id IN (SELECT client_id FROM oauth_clients ORDER BY created_at LIMIT 10)")
    name = str(body.get("client_name") or "Claude")[:80]
    cid = register_client(db, name, uris)
    return JSONResponse({"client_id": cid, "client_name": name, "redirect_uris": uris,
                         "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                         "token_endpoint_auth_method": "none"}, status_code=201)


def _check_authorize(request: Request, p) -> tuple[dict | None, str]:
    db = request.app.state.db
    c = client(db, p.get("client_id", ""))
    if c is None:
        return None, "Nieznana aplikacja."
    if p.get("redirect_uri", "") not in json.loads(c["redirect_uris"]):
        return None, "Niepoprawny adres zwrotny."
    if p.get("response_type") != "code" or p.get("code_challenge_method") != "S256" or len(p.get("code_challenge", "")) < 43:
        return None, "Wymagane PKCE S256."
    res = p.get("resource", "")
    if res and res.rstrip("/") != _resource(request):
        return None, "Niepoprawny zasób."
    return dict(c), ""


def _consent_page(request: Request, c: dict, p, error: str = "") -> HTMLResponse:
    from .app import page_response
    resp = page_response(request, "oauth_zgoda.html", c=c, qp=p, error=error, scope=SCOPE)
    # formularz zgody kończy się przekierowaniem do Claude – CSP form-action musi to dopuścić
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
        "form-action 'self' https://claude.ai https://claude.com; frame-ancestors 'none'; base-uri 'none'")
    return resp


@router.get("/oauth/autoryzuj", response_class=HTMLResponse)
async def authorize(request: Request):
    c, err = _check_authorize(request, request.query_params)
    if c is None:
        return HTMLResponse(f"<p>{escape(err)}</p>", status_code=400)
    return _consent_page(request, c, request.query_params)


@router.post("/oauth/autoryzuj")
async def authorize_post(request: Request):
    from .auth import current_user
    form = await request.form()
    c, err = _check_authorize(request, form)
    if c is None:
        return HTMLResponse(f"<p>{escape(err)}</p>", status_code=400)
    user = current_user(request)
    db = request.app.state.db
    q = {"state": form.get("state", "")} if form.get("state") else {}
    if form.get("decyzja") != "tak":
        q["error"] = "access_denied"
        return RedirectResponse(form["redirect_uri"] + "?" + urlencode(q), status_code=303)
    code = secrets.token_urlsafe(32)
    db.q("INSERT INTO oauth_codes(code_hash, client_id, redirect_uri, challenge, user_email, expires_at) VALUES (?,?,?,?,?,?)",
         (sha(code), c["client_id"], form["redirect_uri"], form["code_challenge"], user["email"],
          iso(now() + timedelta(minutes=CODE_MIN))))
    db.log("mcp_authorized", user["email"], c["name"])
    q["code"] = code
    return RedirectResponse(form["redirect_uri"] + "?" + urlencode(q), status_code=303)


@router.post("/oauth/token")
async def token(request: Request):
    db = request.app.state.db
    form = await request.form()
    gt = form.get("grant_type")
    if gt == "authorization_code":
        row = db.one("SELECT * FROM oauth_codes WHERE code_hash=?", (sha(form.get("code", "")),))
        db.q("DELETE FROM oauth_codes WHERE code_hash=? OR expires_at < ?", (sha(form.get("code", "")), iso(now())))
        if (row is None or row["expires_at"] < iso(now()) or row["client_id"] != form.get("client_id")
                or row["redirect_uri"] != form.get("redirect_uri")
                or not secrets.compare_digest(b64url_sha256(form.get("code_verifier", "")), row["challenge"])):
            return _oauth_err("invalid_grant", "Kod nieważny.")
        if row["user_email"] not in request.app.state.cfg.users:
            return _oauth_err("invalid_grant", "Brak uprawnień.")
        with db.lock:
            gid = db.conn.execute("INSERT INTO oauth_grants(client_id, user_email, created_at) VALUES (?,?,?)",
                                  (row["client_id"], row["user_email"], iso(now()))).lastrowid
        return JSONResponse(issue_tokens(db, gid), headers={"Cache-Control": "no-store"})
    if gt == "refresh_token":
        t = token_grant(db, form.get("refresh_token", ""), "refresh")
        if t is None or t["grant_revoked"] or t["expires_at"] < iso(now()) or t["client_id"] != form.get("client_id"):
            return _oauth_err("invalid_grant", "Token nieważny.")
        if t["used_at"]:                              # ponowne użycie zrotowanego tokenu = kradzież → odcinamy całość
            revoke_grant(db, t["grant_id"])
            db.log("mcp_refresh_reuse", t["user_email"], str(t["grant_id"]))
            return _oauth_err("invalid_grant", "Token już użyty.")
        db.q("UPDATE oauth_tokens SET used_at=? WHERE token_hash=?", (iso(now()), t["token_hash"]))
        return JSONResponse(issue_tokens(db, t["grant_id"]), headers={"Cache-Control": "no-store"})
    return _oauth_err("unsupported_grant_type", "Obsługiwane: authorization_code, refresh_token.")


def _unauthorized(request: Request) -> Response:
    meta = _origin(request) + "/.well-known/oauth-protected-resource"
    return JSONResponse({"error": "invalid_token"}, status_code=401,
                        headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}", scope="{SCOPE}"'})


def _rpc(id_, result=None, error=None) -> dict:
    out = {"jsonrpc": "2.0", "id": id_}
    out.update({"error": error} if error else {"result": result})
    return out


@router.get("/mcp")
async def mcp_get(request: Request):
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp")
async def mcp(request: Request):
    db, app = request.app.state.db, request.app
    h = request.headers.get("authorization", "")
    t = token_grant(db, h[7:].strip(), "access") if h.lower().startswith("bearer ") else None
    if (t is None or t["grant_revoked"] or t["expires_at"] < iso(now())
            or t["user_email"] not in app.state.cfg.users):
        return _unauthorized(request)
    db.q("UPDATE oauth_grants SET last_used_at=? WHERE id=?", (iso(now()), t["grant_id"]))
    try:
        msg = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(_rpc(None, error={"code": -32700, "message": "Parse error"}), status_code=400)
    if not isinstance(msg, dict):
        return JSONResponse(_rpc(None, error={"code": -32600, "message": "Pojedyncze żądania JSON-RPC"}), status_code=400)
    method, id_, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if id_ is None:                                   # powiadomienie
        return Response(status_code=202)
    if method == "initialize":
        v = params.get("protocolVersion")
        return JSONResponse(_rpc(id_, {"protocolVersion": v if v in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[1],
                                       "capabilities": {"tools": {"listChanged": False}},
                                       "serverInfo": {"name": "brandwise-system", "version": "1.0"},
                                       "instructions": ("System firmowy Brandwise. Możesz dopisywać prospekty i "
                                                        "szukać w zleceniach. Przed dodaniem sprawdź duplikaty "
                                                        "narzędziem szukaj.")}))
    if method == "ping":
        return JSONResponse(_rpc(id_, {}))
    if method == "tools/list":
        return JSONResponse(_rpc(id_, {"tools": TOOLS}))
    if method == "tools/call":
        name, args = params.get("name", ""), params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        try:
            text, is_err = call_tool(app, name, args, t["user_email"]), False
        except (ToolError, ValueError) as e:
            text, is_err = str(e), True
        return JSONResponse(_rpc(id_, {"content": [{"type": "text", "text": text}], "isError": is_err}))
    return JSONResponse(_rpc(id_, error={"code": -32601, "message": f"Nieznana metoda {method}"}))
