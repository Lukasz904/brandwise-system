"""Fakturownia: przeterminowane faktury, najbliższe terminy, suma nieopłaconych, oznaczanie opłaconych.

API (dokumentacja: https://github.com/fakturownia/api):
- lista: GET /invoices.json?api_token=…&period=all&kinds[]=…&page=N&per_page=100
- zmiana statusu: POST /invoices/{id}/change_status.json?api_token=…&status=paid
Token wyłącznie z .env (FAKTUROWNIA_API_TOKEN). Lista jest buforowana (domyślnie 5 min).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date

import httpx

from . import Module, Row, Tile, pln

PAID = {"paid"}
SKIPPED = {"rejected"}


def amount(v) -> float:
    """Fakturownia zwraca kwoty jako tekst, czasem z przecinkiem („0,00”)."""
    if v in (None, ""):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return float(str(v).replace(" ", "").replace(",", "."))


@dataclass
class Invoice:
    id: int
    number: str
    buyer: str
    issue_date: date | None
    due: date | None
    gross: float
    net: float
    paid: float
    status: str
    kind: str
    currency: str
    token: str

    @property
    def open_amount(self) -> float:
        return round(max(self.gross - self.paid, 0.0), 2)

    @property
    def unpaid(self) -> bool:
        return self.status not in PAID | SKIPPED and self.open_amount > 0

    def overdue_days(self, today: date) -> int:
        return (today - self.due).days if self.due and self.unpaid and self.due < today else 0


def _d(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def parse(raw: dict) -> Invoice:
    return Invoice(
        id=int(raw["id"]), number=str(raw.get("number") or ""), buyer=str(raw.get("buyer_name") or ""),
        issue_date=_d(raw.get("issue_date")), due=_d(raw.get("payment_to")),
        gross=amount(raw.get("price_gross")), net=amount(raw.get("price_net")), paid=amount(raw.get("paid")),
        status=str(raw.get("status") or ""), kind=str(raw.get("kind") or ""),
        currency=str(raw.get("currency") or "PLN"), token=str(raw.get("token") or ""))


class FakturowniaError(Exception):
    pass


class Fakturownia(Module):
    key = "fakturownia"
    title = "Faktury"
    timeout = 20.0

    def __init__(self, domain: str, token: str, kinds: list[str], cache_seconds: int = 300,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.domain, self.token, self.kinds = domain, token, kinds
        self.cache_seconds = cache_seconds
        self.transport = transport
        self._cache: tuple[float, list[Invoice]] | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.domain and self.token)

    @property
    def base(self) -> str:
        return f"https://{self.domain}.fakturownia.pl"

    def invoice_url(self, inv: Invoice) -> str:
        return f"{self.base}/invoices/{inv.id}"

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, transport=self.transport)

    async def invoices(self, fresh: bool = False) -> list[Invoice]:
        if not self.configured:
            raise FakturowniaError("brak konfiguracji (domena konta i FAKTUROWNIA_API_TOKEN w .env)")
        async with self._lock:
            if not fresh and self._cache and time.monotonic() - self._cache[0] < self.cache_seconds:
                return self._cache[1]
            out: list[Invoice] = []
            async with self.client() as c:
                for page in range(1, 201):                     # bezpiecznik: maks. 20 000 faktur
                    params = [("api_token", self.token), ("period", "all"), ("page", page), ("per_page", 100)]
                    params += [("kinds[]", k) for k in self.kinds]
                    r = await c.get(f"{self.base}/invoices.json", params=params)
                    if r.status_code == 401:
                        raise FakturowniaError("Fakturownia odrzuciła token API")
                    r.raise_for_status()
                    batch = r.json()
                    out += [parse(x) for x in batch if x.get("kind", "") in self.kinds or not x.get("kind")]
                    if len(batch) < 100:
                        break
            self._cache = (time.monotonic(), out)
            return out

    async def mark_paid(self, invoice_id: int) -> None:
        if not self.configured:
            raise FakturowniaError("brak konfiguracji")
        async with self.client() as c:
            r = await c.post(f"{self.base}/invoices/{int(invoice_id)}/change_status.json",
                             params={"api_token": self.token, "status": "paid"})
            if r.status_code >= 400:
                raise FakturowniaError(f"Fakturownia nie zmieniła statusu (HTTP {r.status_code})")
        self._cache = None

    # ---------- obliczenia (czyste, testowalne) ----------
    @staticmethod
    def overview(invs: list[Invoice], today: date) -> dict:
        unpaid = [i for i in invs if i.unpaid]
        overdue = sorted((i for i in unpaid if i.overdue_days(today) > 0), key=lambda i: i.due)
        upcoming = sorted((i for i in unpaid if i.due and i.due >= today), key=lambda i: i.due)
        no_due = [i for i in unpaid if i.due is None]
        pln_only = [i for i in unpaid if i.currency == "PLN"]
        return {
            "unpaid": unpaid,
            "overdue": overdue,
            "upcoming": upcoming,
            "no_due": no_due,
            "total_open": round(sum(i.open_amount for i in pln_only), 2),
            "total_overdue": round(sum(i.open_amount for i in overdue if i.currency == "PLN"), 2),
            "other_currency": sorted({i.currency for i in unpaid if i.currency != "PLN"}),
        }

    async def summary(self) -> Tile:
        o = self.overview(await self.invoices(), date.today())
        t = Tile(key=self.key, title=self.title, href="/faktury", href_label="Wszystkie nieopłacone")
        n_over = len(o["overdue"])
        t.stats = [
            Row("nieopłacone", pln(o["total_open"]), hint=f'{len(o["unpaid"])} faktur, brutto'),
            Row("po terminie", pln(o["total_overdue"]), hint=f"{n_over} faktur", tone="alert" if n_over else "ok"),
        ]
        today = date.today()
        rows = [Row(i.buyer or i.number, pln(i.open_amount), hint=f"{i.number} · {i.overdue_days(today)} dni po terminie",
                    tone="alert", href="/faktury") for i in o["overdue"][:5]]
        rows += [Row(i.buyer or i.number, pln(i.open_amount), hint=f"{i.number} · termin {i.due:%d.%m}",
                     href="/faktury") for i in o["upcoming"][:3]]
        t.rows_title = "Po terminie i 3 najbliższe terminy"
        t.rows = rows
        return t
