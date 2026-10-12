"""System firmowy Brandwise: jeden adres, jedno logowanie (passkeys), wspólny dashboard."""

from __future__ import annotations

import asyncio
from datetime import date
from itertools import groupby
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from . import auth, finance_web
from .config import Config
from .db import DB
from .finance import Finance, month_label, zl
from .modules import pln
from .modules.fakturownia import Fakturownia, FakturowniaError
from .modules.finanse import Finanse
from .modules.marek import Marek
from .modules.prospekty import Prospekty

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["pln"] = pln
templates.env.filters["b64"] = bytes_to_base64url
templates.env.filters["zl"] = zl
templates.env.filters["month_label"] = month_label
templates.env.filters["zlint"] = lambda g: f"{round(g / 100):,}".replace(",", "\u00a0") if g else ""
templates.env.filters["qty"] = lambda v: (f"{v:g}".replace(".", ",") if isinstance(v, (int, float)) else v)
templates.env.filters["fin_choice"] = lambda c, sel: (c["id"], c["name"], c["id"] in sel)
templates.env.filters["str_choice"] = lambda v, sel: (v, v, v in sel)

_ASSET_V: dict[str, str] = {}


def asset(name: str) -> str:
    """URL pliku statycznego z wersją (hash treści) – PWA na iOS nie trzyma starego CSS/JS po wdrożeniu."""
    if name not in _ASSET_V:
        _ASSET_V[name] = __import__("hashlib").sha256((HERE / "static" / name).read_bytes()).hexdigest()[:10]
    return f"/static/{name}?v={_ASSET_V[name]}"


templates.env.globals["asset"] = asset
templates.env.filters["fromjson"] = lambda v: __import__("json").loads(v or "[]")


def page_response(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {"user": auth.current_user(request), **ctx})

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "font-src 'self'; connect-src 'self'; manifest-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
PUBLIC_PREFIXES = ("/static/", "/r/", "/api/widget", "/logowanie", "/zaproszenie/", "/auth/logowanie", "/auth/rejestracja", "/zdrowie")


class Guard(BaseHTTPMiddleware):
    """Origin dla każdego POST (CSRF), nagłówki bezpieczeństwa, przekierowanie niezalogowanych."""

    async def dispatch(self, request: Request, call_next):
        cfg: Config = request.app.state.cfg
        if request.method not in ("GET", "HEAD") and request.headers.get("origin") != cfg.origin:
            return JSONResponse({"detail": "Niepoprawne źródło żądania."}, status_code=403)
        path = request.url.path
        if not path.startswith(PUBLIC_PREFIXES) and auth.current_user(request) is None:
            if request.method in ("GET", "HEAD"):
                return RedirectResponse("/logowanie", status_code=303)
            return JSONResponse({"detail": "Zaloguj się."}, status_code=401)
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            if k == "Cache-Control" and path.startswith("/static/"):
                # wersjonowane (?v=hash) – na zawsze; reszta – zawsze sprawdzaj ETag
                v = ("public, max-age=31536000, immutable" if request.query_params.get("v")
                     else "no-cache")
                resp.headers.setdefault(k, v)
                continue
            resp.headers.setdefault(k, v)
        return resp


def create_app(cfg: Config, fakturownia: Fakturownia | None = None, marek: Marek | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.db = DB(cfg.db_path)
    app.state.fakturownia = fakturownia or Fakturownia(cfg.fakturownia_domain, cfg.fakturownia_token,
                                                       cfg.fakturownia_kinds, cfg.fakturownia_cache_seconds,
                                                       hidden_buyers=cfg.fakturownia_hidden)
    app.state.finance = Finance(app.state.db)
    app.state.modules = [marek or Marek(cfg.marek_summary_url, cfg.marek_host), app.state.fakturownia,
                         Finanse(app.state.finance), Prospekty(app.state.finance)]
    app.add_middleware(Guard)
    app.include_router(auth.router)
    app.include_router(finance_web.router)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    page = page_response

    @app.get("/zdrowie")
    async def health():
        return {"ok": True}

    @app.get("/logowanie", response_class=HTMLResponse)
    async def login_page(request: Request):
        if auth.current_user(request):
            return RedirectResponse("/", status_code=303)
        return page(request, "logowanie.html")

    @app.get("/zaproszenie/{token}", response_class=HTMLResponse)
    async def invite_page(request: Request, token: str):
        inv = app.state.db.invite(token)
        return page(request, "zaproszenie.html", invite=inv, token=token)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        tiles = await asyncio.gather(*(m.safe_summary() for m in app.state.modules))
        return page(request, "dashboard.html", tiles=tiles, today=date.today())

    @app.get("/faktury", response_class=HTMLResponse)
    async def invoices(request: Request, odswiez: int = 0):
        fk: Fakturownia = app.state.fakturownia
        try:
            o = fk.overview(await fk.invoices(fresh=bool(odswiez)), date.today())
            error = ""
        except (FakturowniaError, Exception) as e:  # noqa: BLE001
            o, error = None, str(e) or type(e).__name__
        return page(request, "faktury.html", o=o, error=error, fk=fk, today=date.today(),
                    msg=request.query_params.get("ok", ""))

    @app.post("/faktury/{invoice_id}/oplacona")
    async def mark_paid(request: Request, invoice_id: int):
        user = auth.current_user(request)
        fk: Fakturownia = app.state.fakturownia
        try:
            await fk.mark_paid(invoice_id)
        except FakturowniaError as e:
            raise HTTPException(502, str(e)) from e
        app.state.db.log("invoice_marked_paid", user["email"], str(invoice_id))
        return RedirectResponse("/faktury?ok=1", status_code=303)

    @app.get("/klucze", response_class=HTMLResponse)
    async def keys(request: Request):
        user = auth.current_user(request)
        return page(request, "klucze.html", creds=app.state.db.credentials_of(user["id"]),
                    tokens=app.state.db.api_tokens(), new_token="")

    @app.post("/klucze/widget", response_class=HTMLResponse)
    async def widget_token(request: Request):
        user = auth.current_user(request)
        form = await request.form()
        token = app.state.db.create_api_token(form.get("name", "iPhone"))
        app.state.db.log("widget_token_created", user["email"], form.get("name", "")[:60])
        return page(request, "klucze.html", creds=app.state.db.credentials_of(user["id"]),
                    tokens=app.state.db.api_tokens(), new_token=token)

    @app.post("/klucze/widget/{tid}/usun")
    async def widget_token_revoke(request: Request, tid: int):
        user = auth.current_user(request)
        app.state.db.revoke_api_token(tid)
        app.state.db.log("widget_token_revoked", user["email"], str(tid))
        return RedirectResponse("/klucze", status_code=303)

    @app.get("/api/widget")
    async def widget_api(request: Request):
        """Podsumowanie dla widgetu (Scriptable). Tylko odczyt, token w nagłówku Authorization: Bearer …"""
        auth_h = request.headers.get("authorization", "")
        token = auth_h[7:].strip() if auth_h.lower().startswith("bearer ") else ""
        if not token or app.state.db.api_token(token, "widget") is None:
            return JSONResponse({"detail": "Brak dostępu."}, status_code=401)
        from .finance import Filters, prev_month  # noqa: F401
        F = app.state.finance
        month = date.today().strftime("%Y-%m")
        s = F.month_summary(month)
        todo = F.totals(Filters(invoice_status=("Brak",)))
        prosp = F.totals(Filters(kind="prospekt"))
        out = {"miesiac": s["label"], "przychod": s["revenue"], "dochod": s["profit"],
               "do_zafakturowania": todo["total"], "do_zafakturowania_n": todo["n"],
               "prospekty": prosp["total"], "prospekty_n": prosp["n"], "faktury": None, "marek": None,
               "url": cfg.origin + "/"}
        try:
            fk = app.state.fakturownia
            o = fk.overview(await asyncio.wait_for(fk.invoices(), 15), date.today())
            out["faktury"] = {"nieoplacone": round(o["total_open"] * 100), "po_terminie": round(o["total_overdue"] * 100),
                              "po_terminie_n": len(o["overdue"]), "najblizszy": (o["upcoming"][0].due.isoformat()
                                                                                 if o["upcoming"] else None)}
        except Exception:  # noqa: BLE001 – widget pokazuje resztę
            pass
        try:
            m = await asyncio.wait_for(app.state.modules[0].fetch(), 5)
            out["marek"] = {"czeka": m.get("states", {}).get("czeka na Ciebie", 0), "stop": bool(m.get("stopped"))}
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post("/klucze/{cred_id}/usun")
    async def delete_key(request: Request, cred_id: str):
        user = auth.current_user(request)
        if not app.state.db.delete_credential(user["id"], base64url_to_bytes(cred_id)):
            raise HTTPException(400, "Nie można usunąć ostatniego klucza.")
        app.state.db.log("passkey_deleted", user["email"], cred_id[:16])
        return RedirectResponse("/klucze", status_code=303)

    return app
