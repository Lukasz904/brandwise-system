"""Pełny przepływ passkey z programowym uwierzytelniaczem (ES256, flagi UP+UV): zaproszenie → klucz → sesja →
wylogowanie → logowanie kluczem. Sprawdza też, że zaproszenie jest jednorazowe, a podpis spoza klucza odpada."""

import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from .conftest import ORIGIN

RP = "panel.brandwise.pl"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Authenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.user_handle = b""
        self.count = 0

    def _auth_data(self, attested: bool) -> bytes:
        self.count += 1
        flags = 0x45 if attested else 0x05          # UP + UV (+ AT)
        data = hashlib.sha256(RP.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count)
        if attested:
            n = self.key.public_key().public_numbers()
            cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})
            data += b"\x00" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + cose
        return data

    def create(self, options: dict) -> dict:
        self.user_handle = unb64(options["user"]["id"])
        cd = json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": ORIGIN}).encode()
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": self._auth_data(True)})
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(cd), "attestationObject": b64(att)}, "clientExtensionResults": {}}

    def get(self, options: dict, key=None) -> dict:
        cd = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": ORIGIN}).encode()
        ad = self._auth_data(False)
        sig = (key or self.key).sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(cd), "authenticatorData": b64(ad), "signature": b64(sig),
                             "userHandle": b64(self.user_handle)}, "clientExtensionResults": {}}


def test_full_passkey_flow(make_client):
    c, app = make_client()
    h = {"Origin": ORIGIN}
    dev = Authenticator()
    invite = app.state.db.create_invite("lukasz@brandwise.pl")

    o = c.post("/auth/rejestracja/opcje", json={"zaproszenie": invite}, headers=h).json()
    r = c.post("/auth/rejestracja", json={"zaproszenie": invite, "nazwa": "MacBook", "credential": dev.create(o)},
               headers=h)
    assert r.status_code == 200, r.text
    assert c.get("/").status_code == 200                       # zalogowany od razu po utworzeniu klucza
    assert "MacBook" in c.get("/klucze").text

    assert app.state.db.invite(invite) is None                 # zaproszenie jednorazowe

    c.post("/wyloguj", headers=h)
    c.cookies.clear()
    assert c.get("/").status_code == 303

    # logowanie kluczem
    o = c.post("/auth/logowanie/opcje", headers=h).json()
    r = c.post("/auth/logowanie", json={"credential": dev.get(o)}, headers=h)
    assert r.status_code == 200, r.text
    assert c.get("/").status_code == 200

    # podpis innym kluczem prywatnym – odrzucony
    c.cookies.clear()
    o = c.post("/auth/logowanie/opcje", headers=h).json()
    forged = dev.get(o, key=ec.generate_private_key(ec.SECP256R1()))
    assert c.post("/auth/logowanie", json={"credential": forged}, headers=h).status_code == 403

    # powtórka tego samego wyzwania – odrzucona (wyzwanie jednorazowe)
    o = c.post("/auth/logowanie/opcje", headers=h).json()
    good = dev.get(o)
    assert c.post("/auth/logowanie", json={"credential": good}, headers=h).status_code == 200
    c.cookies.pop("bw_system")
    assert c.post("/auth/logowanie", json={"credential": good}, headers=h).status_code == 400
