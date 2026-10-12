// Passkeys (WebAuthn) + potwierdzenia formularzy. Bez skryptów inline (CSP: script-src 'self').
"use strict";

const b64u = {
  toBuf(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    const bin = atob(s + "===".slice((s.length + 3) % 4));
    return Uint8Array.from(bin, (c) => c.charCodeAt(0)).buffer;
  },
  fromBuf(b) {
    const s = btoa(String.fromCharCode(...new Uint8Array(b)));
    return s.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
};

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || "Coś poszło nie tak.");
  return data;
}

function show(msg) {
  const el = document.getElementById("msg");
  if (el) el.textContent = msg;
}

function friendly(e) {
  if (e && e.name === "NotAllowedError") return "Anulowano albo czas minął.";
  if (e && e.name === "InvalidStateError") return "Ten klucz jest już zapisany.";
  return (e && e.message) || "Coś poszło nie tak.";
}

async function login() {
  show("");
  try {
    const o = await post("/auth/logowanie/opcje");
    o.challenge = b64u.toBuf(o.challenge);
    (o.allowCredentials || []).forEach((c) => (c.id = b64u.toBuf(c.id)));
    const cred = await navigator.credentials.get({ publicKey: o });
    await post("/auth/logowanie", {
      credential: {
        id: cred.id, rawId: b64u.fromBuf(cred.rawId), type: cred.type,
        response: {
          clientDataJSON: b64u.fromBuf(cred.response.clientDataJSON),
          authenticatorData: b64u.fromBuf(cred.response.authenticatorData),
          signature: b64u.fromBuf(cred.response.signature),
          userHandle: cred.response.userHandle ? b64u.fromBuf(cred.response.userHandle) : null,
        },
        clientExtensionResults: cred.getClientExtensionResults(),
      },
    });
    const btn = document.querySelector("[data-passkey-login]");
    const next = (btn && btn.dataset.next) || "";
    location.href = next.startsWith("/") && !next.startsWith("//") && !next.includes("\\") ? next : "/";
  } catch (e) { show(friendly(e)); }
}

async function register(invite) {
  show("");
  const labelEl = document.getElementById("label");
  const label = labelEl ? labelEl.value.trim() : "";
  try {
    const o = await post("/auth/rejestracja/opcje", { zaproszenie: invite });
    o.challenge = b64u.toBuf(o.challenge);
    o.user.id = b64u.toBuf(o.user.id);
    (o.excludeCredentials || []).forEach((c) => (c.id = b64u.toBuf(c.id)));
    const cred = await navigator.credentials.create({ publicKey: o });
    await post("/auth/rejestracja", {
      zaproszenie: invite, nazwa: label,
      credential: {
        id: cred.id, rawId: b64u.fromBuf(cred.rawId), type: cred.type,
        response: {
          clientDataJSON: b64u.fromBuf(cred.response.clientDataJSON),
          attestationObject: b64u.fromBuf(cred.response.attestationObject),
          transports: cred.response.getTransports ? cred.response.getTransports() : [],
        },
        clientExtensionResults: cred.getClientExtensionResults(),
      },
    });
    location.href = invite ? "/" : "/klucze";
  } catch (e) { show(friendly(e)); }
}

document.addEventListener("DOMContentLoaded", () => {
  const l = document.querySelector("[data-passkey-login]");
  if (l) l.addEventListener("click", login);
  const r = document.querySelector("[data-passkey-register]");
  if (r) r.addEventListener("click", () => register(r.dataset.invite || ""));
  document.querySelectorAll("form[data-confirm]").forEach((f) =>
    f.addEventListener("submit", (ev) => { if (!confirm(f.dataset.confirm)) ev.preventDefault(); }));
  const all = document.querySelector("[data-check-all]");
  if (all) all.addEventListener("change", () =>
    document.querySelectorAll('#bulk input[name="id"]').forEach((c) => (c.checked = all.checked)));
  const allc = document.querySelector("[data-check-all-costs]");
  if (allc) allc.addEventListener("change", () =>
    allc.closest("form").querySelectorAll('input[name="id"]').forEach((c) => (c.checked = allc.checked)));
  const add = document.querySelector("[data-add-cost]");
  if (add) add.addEventListener("click", () => {
    const box = document.querySelector("[data-costs]");
    const row = box.lastElementChild.cloneNode(true);
    row.querySelectorAll("input").forEach((i) => (i.value = ""));
    box.appendChild(row);
  });
  document.querySelectorAll("[data-toggle]").forEach((b) => {
    const label = b.textContent;
    b.addEventListener("click", () => {
      let open = false;
      document.querySelectorAll(`[data-fold="${b.dataset.toggle}"]`).forEach((el) => (open = el.classList.toggle("open")));
      b.setAttribute("aria-expanded", open ? "true" : "false");
      b.textContent = open ? (b.dataset.labelOpen || label) : label;
    });
  });
  document.addEventListener("click", (ev) => {
    document.querySelectorAll("details.dd[open]").forEach((d) => { if (!d.contains(ev.target)) d.open = false; });
  });
  if (!window.PublicKeyCredential && (l || r)) show("Ta przeglądarka nie obsługuje kluczy dostępu.");
});
