"""Logowanie passkeys (WebAuthn): klucz w pęku kluczy iCloud, potwierdzenie Touch ID / Face ID, bez hasła.

- Pierwszy klucz: zaproszenie z CLI na serwerze (`python -m brandwise_system zaproszenie <email>`), ważne 30 min.
- Kolejne urządzenia: po zalogowaniu, w „Klucze dostępu” (iCloud i tak synchronizuje klucz między Makiem a iPhone'em).
- Sesja: losowy token w ciasteczku HttpOnly/Secure/SameSite=Lax, w bazie tylko skrót.
- Każdy POST musi mieć nagłówek Origin zgodny z adresem systemu (ochrona CSRF) – patrz app.py.
"""

from __future__ import annotations

import json

import webauthn
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

SESSION_COOKIE = "bw_system"
CHALLENGE_COOKIE = "bw_chal"
RP_NAME = "Brandwise"


def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return request.app.state.db.session_user(token)


def _rp_id(request: Request) -> str:
    return request.app.state.cfg.host


def _json_with_challenge(request: Request, options, purpose: str, email: str | None) -> JSONResponse:
    db = request.app.state.db
    cid = db.put_challenge(options.challenge, purpose, email)
    resp = JSONResponse(json.loads(webauthn.options_to_json(options)))
    resp.set_cookie(CHALLENGE_COOKIE, cid, max_age=300, httponly=True, secure=True, samesite="strict")
    return resp


def _session_response(request: Request, user_id: int, payload: dict) -> JSONResponse:
    cfg, db = request.app.state.cfg, request.app.state.db
    token = db.new_session(user_id, cfg.session_days)
    resp = JSONResponse(payload)
    resp.set_cookie(SESSION_COOKIE, token, max_age=cfg.session_days * 86400, httponly=True, secure=True,
                    samesite="lax")
    resp.delete_cookie(CHALLENGE_COOKIE)
    return resp


router = APIRouter()


# ---------- rejestracja klucza ----------
@router.post("/auth/rejestracja/opcje")
async def register_options(request: Request):
    cfg, db = request.app.state.cfg, request.app.state.db
    body = await request.json()
    user = current_user(request)
    if user is None:                                   # pierwszy klucz – tylko z ważnym zaproszeniem
        inv = db.invite(str(body.get("zaproszenie", "")))
        if inv is None or inv["email"] not in cfg.users:
            raise HTTPException(403, "Zaproszenie wygasło albo jest niepoprawne.")
        email = inv["email"]
        user = db.ensure_user(email)
    email = user["email"]
    options = webauthn.generate_registration_options(
        rp_id=_rp_id(request), rp_name=RP_NAME, user_name=email, user_display_name=email,
        user_id=user["handle"],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=c["id"]) for c in db.credentials_of(user["id"])],
    )
    return _json_with_challenge(request, options, "register", email)


@router.post("/auth/rejestracja")
async def register_verify(request: Request):
    cfg, db = request.app.state.cfg, request.app.state.db
    body = await request.json()
    chal = db.take_challenge(request.cookies.get(CHALLENGE_COOKIE, ""), "register")
    if chal is None:
        raise HTTPException(400, "Czas na potwierdzenie minął – spróbuj jeszcze raz.")
    logged = current_user(request)
    invite_token = str(body.get("zaproszenie", ""))
    if logged is None:
        inv = db.invite(invite_token)
        if inv is None or inv["email"] != chal["email"]:
            raise HTTPException(403, "Zaproszenie wygasło albo jest niepoprawne.")
    elif logged["email"] != chal["email"]:
        raise HTTPException(403, "Niezgodna sesja.")
    try:
        v = webauthn.verify_registration_response(
            credential=body["credential"], expected_challenge=chal["challenge"],
            expected_rp_id=_rp_id(request), expected_origin=cfg.origin, require_user_verification=True)
    except Exception as e:  # noqa: BLE001 – biblioteka rzuca różne typy; użytkownik dostaje jeden komunikat
        db.log("passkey_register_failed", chal["email"], type(e).__name__)
        raise HTTPException(400, "Nie udało się zapisać klucza.") from e
    user = db.ensure_user(chal["email"])
    label = str(body.get("nazwa") or "").strip()[:60] or "Klucz dostępu"
    db.add_credential(user["id"], v.credential_id, v.credential_public_key, v.sign_count, label)
    db.log("passkey_registered", user["email"], label)
    if logged is None:
        db.use_invite(invite_token)
        return _session_response(request, user["id"], {"ok": True})
    return JSONResponse({"ok": True})


# ---------- logowanie ----------
@router.post("/auth/logowanie/opcje")
async def login_options(request: Request):
    options = webauthn.generate_authentication_options(
        rp_id=_rp_id(request), user_verification=UserVerificationRequirement.REQUIRED)
    return _json_with_challenge(request, options, "login", None)


@router.post("/auth/logowanie")
async def login_verify(request: Request):
    cfg, db = request.app.state.cfg, request.app.state.db
    body = await request.json()
    chal = db.take_challenge(request.cookies.get(CHALLENGE_COOKIE, ""), "login")
    if chal is None:
        raise HTTPException(400, "Czas na potwierdzenie minął – spróbuj jeszcze raz.")
    cred = body.get("credential") or {}
    try:
        cred_id = base64url_to_bytes(str(cred.get("rawId") or cred.get("id") or ""))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, "Niepoprawny klucz.") from e
    row = db.credential(cred_id)
    user = db.user(row["user_id"]) if row else None
    if row is None or user is None or user["email"] not in cfg.users:
        db.log("login_unknown_credential", None, bytes_to_base64url(cred_id)[:16])
        raise HTTPException(403, "Ten klucz nie ma dostępu do systemu.")
    try:
        v = webauthn.verify_authentication_response(
            credential=cred, expected_challenge=chal["challenge"], expected_rp_id=_rp_id(request),
            expected_origin=cfg.origin, credential_public_key=row["public_key"],
            credential_current_sign_count=row["sign_count"], require_user_verification=True)
    except Exception as e:  # noqa: BLE001
        db.log("login_failed", user["email"], type(e).__name__)
        raise HTTPException(403, "Logowanie nieudane.") from e
    db.touch_credential(cred_id, v.new_sign_count)
    db.log("login", user["email"], row["label"])
    return _session_response(request, user["id"], {"ok": True})


@router.post("/wyloguj")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        request.app.state.db.end_session(token)
    resp = RedirectResponse("/logowanie", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
