"""Panel finansowy: zlecenia, klienci, koszty miesięczne, statusy, raporty dla klientów.

Kwoty netto, przechowywane w groszach (int). Data zlecenia jako ISO (RRRR-MM-DD).
Zlecenie ma klienta (dla kogo) i płatnika (komu fakturujemy) – w starym panelu „client” i „invoice”.
`account_legacy` to nazwa accounta ze starego panelu (archiwum); nowe zlecenia go nie mają.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS fin_clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS fin_orders (
    id INTEGER PRIMARY KEY,
    hashtag TEXT NOT NULL DEFAULT '',          -- numer zadania RedMine / umowy
    client_id INTEGER NOT NULL REFERENCES fin_clients(id),
    payer_id INTEGER NOT NULL REFERENCES fin_clients(id),
    day TEXT NOT NULL,                         -- RRRR-MM-DD
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    principal TEXT NOT NULL DEFAULT '',        -- zleceniodawca (osoba po stronie klienta)
    price INTEGER NOT NULL DEFAULT 0,          -- grosze netto
    qty REAL NOT NULL DEFAULT 1,
    total INTEGER NOT NULL DEFAULT 0,          -- grosze netto = price × qty
    costs TEXT NOT NULL DEFAULT '[]',          -- [[nazwa, grosze], …] koszty zlecenia
    costs_total INTEGER NOT NULL DEFAULT 0,
    invoice_status TEXT NOT NULL DEFAULT '',
    account_legacy TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS fin_orders_day ON fin_orders(day);
CREATE TABLE IF NOT EXISTS fin_costs (
    id INTEGER PRIMARY KEY,
    month TEXT NOT NULL,                       -- RRRR-MM
    name TEXT NOT NULL,
    value INTEGER NOT NULL                     -- grosze netto
);
CREATE INDEX IF NOT EXISTS fin_costs_month ON fin_costs(month);
CREATE TABLE IF NOT EXISTS fin_settings (
    name TEXT PRIMARY KEY,                     -- status | invoice_status
    value TEXT NOT NULL                        -- JSON lista
);
CREATE TABLE IF NOT EXISTS fin_reports (
    token TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    filters TEXT NOT NULL,                     -- JSON
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT
);
"""

DEFAULT_STATUSES = ["Wycena", "Projektowanie", "Akceptacja", "Wdrożenie", "Zakończone"]
DEFAULT_INVOICE_STATUSES = ["Brak", "Wysłane", "Zaakceptowane", "Faktura"]
INVOICED = "Faktura"
TO_INVOICE = "Brak"
FILTER_LISTS = ("client_id", "payer_id", "status", "invoice_status")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_grosze(text) -> int:
    """'1 234,50' / '1234.5' / 1234.5 → 123450. Puste → 0."""
    if text in (None, ""):
        return 0
    if isinstance(text, (int, float)):
        return round(float(text) * 100)
    s = str(text).replace(" ", "").replace(" ", "").replace(",", ".")
    return round(float(s) * 100) if s else 0


def to_qty(text) -> float:
    if text in (None, ""):
        return 1.0
    return float(str(text).replace(" ", "").replace(",", "."))


def zl(grosze: int | None) -> str:
    if grosze is None:
        return "—"
    neg = grosze < 0
    g = abs(int(grosze))
    s = f"{g // 100:,}".replace(",", " ") + f",{g % 100:02d}"
    return f"{'−' if neg else ''}{s} zł"


def month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return f"{y:04d}-{m:02d}-01", nxt.isoformat()


def prev_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - (m == 1):04d}-{12 if m == 1 else m - 1:02d}"


MONTHS_PL = ["styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień", "wrzesień",
             "październik", "listopad", "grudzień"]


def month_label(month: str) -> str:
    y, m = month.split("-")
    return f"{MONTHS_PL[int(m) - 1]} {y}"


@dataclass
class Filters:
    day_from: str = ""
    day_to: str = ""
    client_id: tuple[int, ...] = ()
    payer_id: tuple[int, ...] = ()
    status: tuple[str, ...] = ()
    invoice_status: tuple[str, ...] = ()
    principal: str = ""
    hashtag: str = ""
    priced_only: bool = False
    q: str = ""

    @classmethod
    def from_query(cls, qp) -> "Filters":
        """qp: dowolny obiekt z .getlist() / .get() (QueryParams, dict list) albo dict z JSON-a."""
        def lst(k):
            if hasattr(qp, "getlist"):
                return [v for v in qp.getlist(k) if v != ""]
            v = qp.get(k, [])
            return [x for x in (v if isinstance(v, list) else [v]) if x != ""]
        def one(k):
            v = qp.get(k, "")
            return (v[0] if isinstance(v, list) and v else v) or ""
        return cls(
            day_from=one("od"), day_to=one("do"),
            client_id=tuple(int(x) for x in lst("klient") if str(x).isdigit()),
            payer_id=tuple(int(x) for x in lst("platnik") if str(x).isdigit()),
            status=tuple(lst("status")), invoice_status=tuple(lst("faktura")),
            principal=one("zleceniodawca").strip(), hashtag=one("hashtag").strip(),
            priced_only=one("wycenione") in ("1", "on", True), q=one("q").strip())

    def to_query(self) -> dict:
        d: dict = {}
        if self.day_from: d["od"] = self.day_from
        if self.day_to: d["do"] = self.day_to
        if self.client_id: d["klient"] = list(map(str, self.client_id))
        if self.payer_id: d["platnik"] = list(map(str, self.payer_id))
        if self.status: d["status"] = list(self.status)
        if self.invoice_status: d["faktura"] = list(self.invoice_status)
        if self.principal: d["zleceniodawca"] = self.principal
        if self.hashtag: d["hashtag"] = self.hashtag
        if self.priced_only: d["wycenione"] = "1"
        if self.q: d["q"] = self.q
        return d

    @property
    def empty(self) -> bool:
        return not self.to_query()

    def sql(self) -> tuple[str, list]:
        where, args = ["o.deleted_at IS NULL"], []
        if self.day_from:
            where.append("o.day >= ?"); args.append(self.day_from)
        if self.day_to:
            where.append("o.day <= ?"); args.append(self.day_to)
        for col, vals in (("client_id", self.client_id), ("payer_id", self.payer_id), ("status", self.status),
                          ("invoice_status", self.invoice_status)):
            if vals:
                where.append(f"o.{col} IN ({','.join('?' * len(vals))})"); args += list(vals)
        if self.principal:
            where.append("o.principal LIKE ?"); args.append(f"%{self.principal}%")
        if self.hashtag:
            where.append("o.hashtag = ?"); args.append(self.hashtag)
        if self.priced_only:
            where.append("o.price > 0")
        if self.q:
            where.append("(o.title LIKE ? OR o.hashtag LIKE ? OR o.principal LIKE ?)"); args += [f"%{self.q}%"] * 3
        return " AND ".join(where), args


class Finance:
    def __init__(self, db):
        self.db = db
        with db.lock:
            db.conn.executescript(SCHEMA)
        if not self.db.one("SELECT 1 FROM fin_settings WHERE name='status'"):
            self.set_list("status", DEFAULT_STATUSES)
        if not self.db.one("SELECT 1 FROM fin_settings WHERE name='invoice_status'"):
            self.set_list("invoice_status", DEFAULT_INVOICE_STATUSES)

    # ---------- ustawienia ----------
    def get_list(self, name: str) -> list[str]:
        row = self.db.one("SELECT value FROM fin_settings WHERE name=?", (name,))
        return json.loads(row["value"]) if row else []

    def set_list(self, name: str, values: list[str]) -> None:
        clean = [v.strip() for v in values if v.strip()]
        self.db.q("INSERT INTO fin_settings(name, value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                  (name, json.dumps(clean, ensure_ascii=False)))

    # ---------- klienci ----------
    def clients(self, with_deleted: bool = False):
        sql = "SELECT * FROM fin_clients" + ("" if with_deleted else " WHERE deleted_at IS NULL") + " ORDER BY name COLLATE NOCASE"
        return self.db.q(sql)

    def client_names(self) -> dict[int, str]:
        return {r["id"]: r["name"] for r in self.clients(with_deleted=True)}

    def add_client(self, name: str) -> int:
        name = name.strip()
        if not name:
            raise ValueError("Podaj nazwę klienta.")
        with self.db.lock:
            cur = self.db.conn.execute("INSERT INTO fin_clients(name) VALUES (?)", (name,))
            return cur.lastrowid

    def rename_client(self, cid: int, name: str) -> None:
        if not name.strip():
            raise ValueError("Podaj nazwę klienta.")
        self.db.q("UPDATE fin_clients SET name=? WHERE id=?", (name.strip(), cid))

    def delete_client(self, cid: int) -> None:
        self.db.q("UPDATE fin_clients SET deleted_at=? WHERE id=?", (now_iso(), cid))

    # ---------- zlecenia ----------
    def order(self, oid: int):
        return self.db.one("SELECT * FROM fin_orders WHERE id=? AND deleted_at IS NULL", (oid,))

    def orders(self, f: Filters, limit: int | None = 500):
        where, args = f.sql()
        sql = (f"SELECT o.*, c.name AS client_name, p.name AS payer_name FROM fin_orders o "
               f"JOIN fin_clients c ON c.id=o.client_id JOIN fin_clients p ON p.id=o.payer_id "
               f"WHERE {where} ORDER BY o.day DESC, o.id DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.db.q(sql, args)

    def totals(self, f: Filters) -> dict:
        where, args = f.sql()
        r = self.db.one(f"SELECT COUNT(*) n, COALESCE(SUM(total),0) total, COALESCE(SUM(costs_total),0) costs "
                        f"FROM fin_orders o WHERE {where}", args)
        return {"n": r["n"], "total": r["total"], "costs": r["costs"], "net": r["total"] - r["costs"]}

    def save_order(self, data: dict, oid: int | None = None) -> int:
        """data: hashtag, client_id, payer_id, day, title, status, principal, price(gr), qty, invoice_status,
        costs: [[nazwa, grosze]]"""
        if not data.get("title", "").strip():
            raise ValueError("Podaj tytuł zlecenia.")
        for k in ("client_id", "payer_id"):
            if not self.db.one("SELECT 1 FROM fin_clients WHERE id=?", (data.get(k),)):
                raise ValueError("Wybierz klienta i płatnika.")
        try:
            date.fromisoformat(data.get("day", ""))
        except ValueError as e:
            raise ValueError("Niepoprawna data.") from e
        costs = [[n.strip(), int(v)] for n, v in data.get("costs", []) if n.strip() or v]
        row = {
            "hashtag": data.get("hashtag", "").strip(), "client_id": int(data["client_id"]),
            "payer_id": int(data["payer_id"]), "day": data["day"], "title": data["title"].strip(),
            "status": data.get("status", ""), "principal": data.get("principal", "").strip(),
            "price": int(data.get("price", 0)), "qty": float(data.get("qty", 1)),
            "invoice_status": data.get("invoice_status", ""),
            "costs": json.dumps(costs, ensure_ascii=False), "costs_total": sum(v for _, v in costs),
        }
        row["total"] = round(row["price"] * row["qty"])
        ts = now_iso()
        with self.db.lock:
            if oid is None:
                cols = list(row) + ["created_at", "updated_at"]
                cur = self.db.conn.execute(
                    f"INSERT INTO fin_orders({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [*row.values(), ts, ts])
                return cur.lastrowid
            sets = ",".join(f"{k}=?" for k in row) + ",updated_at=?"
            self.db.conn.execute(f"UPDATE fin_orders SET {sets} WHERE id=?", [*row.values(), ts, oid])
            return oid

    def bulk_update(self, ids: list[int], status: str = "", invoice_status: str = "", day: str = "") -> int:
        sets, args = [], []
        if status: sets.append("status=?"); args.append(status)
        if invoice_status: sets.append("invoice_status=?"); args.append(invoice_status)
        if day:
            date.fromisoformat(day); sets.append("day=?"); args.append(day)
        if not sets or not ids:
            return 0
        sets.append("updated_at=?"); args.append(now_iso())
        self.db.q(f"UPDATE fin_orders SET {','.join(sets)} WHERE deleted_at IS NULL AND id IN "
                  f"({','.join('?' * len(ids))})", args + list(ids))
        return len(ids)

    def delete_order(self, oid: int) -> None:
        self.db.q("UPDATE fin_orders SET deleted_at=? WHERE id=?", (now_iso(), oid))

    # ---------- koszty miesięczne ----------
    def costs(self, month: str):
        return self.db.q("SELECT * FROM fin_costs WHERE month=? ORDER BY name COLLATE NOCASE", (month,))

    def cost_months(self) -> list[str]:
        return [r["month"] for r in self.db.q("SELECT DISTINCT month FROM fin_costs ORDER BY month DESC")]

    def add_cost(self, month: str, name: str, value: int) -> None:
        datetime.strptime(month, "%Y-%m")
        if not name.strip():
            raise ValueError("Podaj nazwę kosztu.")
        self.db.q("INSERT INTO fin_costs(month, name, value) VALUES (?,?,?)", (month, name.strip(), int(value)))

    def update_cost(self, cid: int, name: str, value: int) -> None:
        self.db.q("UPDATE fin_costs SET name=?, value=? WHERE id=?", (name.strip(), int(value), cid))

    def delete_cost(self, cid: int) -> None:
        self.db.q("DELETE FROM fin_costs WHERE id=?", (cid,))

    def copy_costs(self, src: str, dst: str) -> int:
        datetime.strptime(dst, "%Y-%m")
        rows = self.costs(src)
        for r in rows:
            self.add_cost(dst, r["name"], r["value"])
        return len(rows)

    # ---------- podsumowania ----------
    def month_summary(self, month: str) -> dict:
        a, b = month_bounds(month)
        r = self.db.one("SELECT COALESCE(SUM(total),0) t, COALESCE(SUM(costs_total),0) c, COUNT(*) n, "
                        "COALESCE(SUM(CASE WHEN invoice_status=? THEN total-costs_total END),0) inv_t, "
                        "COALESCE(SUM(CASE WHEN invoice_status=? THEN 1 END),0) inv_n, "
                        "COALESCE(SUM(CASE WHEN invoice_status=? THEN total END),0) todo_t, "
                        "COALESCE(SUM(CASE WHEN invoice_status=? THEN 1 END),0) todo_n "
                        "FROM fin_orders WHERE deleted_at IS NULL AND day>=? AND day<?",
                        (INVOICED, INVOICED, TO_INVOICE, TO_INVOICE, a, b))
        fixed = self.db.one("SELECT COALESCE(SUM(value),0) v FROM fin_costs WHERE month=?", (month,))["v"]
        revenue = r["t"] - r["c"]                     # przychód po kosztach zleceń
        return {
            "month": month, "label": month_label(month), "orders": r["n"], "sales": r["t"],
            "order_costs": r["c"], "revenue": revenue, "fixed_costs": fixed, "profit": revenue - fixed,
            "invoiced_pct_amount": round(r["inv_t"] / revenue * 100) if revenue else 0,
            "invoiced_pct_count": round(r["inv_n"] / r["n"] * 100) if r["n"] else 0,
            "to_invoice_total": r["todo_t"], "to_invoice_count": r["todo_n"],
        }

    def by_client(self, month: str) -> list[tuple[str, int]]:
        a, b = month_bounds(month)
        rows = self.db.q("SELECT c.name, SUM(o.total - o.costs_total) v FROM fin_orders o JOIN fin_clients c "
                         "ON c.id=o.client_id WHERE o.deleted_at IS NULL AND o.day>=? AND o.day<? "
                         "GROUP BY c.id ORDER BY v DESC", (a, b))
        return [(r["name"], r["v"]) for r in rows]

    def history(self, months: int = 24) -> list[dict]:
        rows = self.db.q("SELECT substr(day,1,7) m, SUM(total-costs_total) rev FROM fin_orders "
                         "WHERE deleted_at IS NULL GROUP BY m ORDER BY m DESC LIMIT ?", (months,))
        fixed = {r["month"]: r["v"] for r in self.db.q("SELECT month, SUM(value) v FROM fin_costs GROUP BY month")}
        return [{"month": r["m"], "label": month_label(r["m"]), "revenue": r["rev"], "fixed_costs": fixed.get(r["m"], 0),
                 "profit": r["rev"] - fixed.get(r["m"], 0)} for r in rows]

    def year_table(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for r in self.db.q("SELECT substr(day,1,4) y, CAST(substr(day,6,2) AS INT) m, SUM(total-costs_total) v "
                           "FROM fin_orders WHERE deleted_at IS NULL GROUP BY y, m ORDER BY y DESC"):
            out.setdefault(r["y"], [0] * 12)[r["m"] - 1] = r["v"]
        return out

    # ---------- raporty dla klientów ----------
    def create_report(self, f: Filters, title: str = "", expires_at: str | None = None) -> str:
        token = secrets.token_urlsafe(24)
        self.db.q("INSERT INTO fin_reports(token, title, filters, created_at, expires_at) VALUES (?,?,?,?,?)",
                  (token, title.strip(), json.dumps(f.to_query(), ensure_ascii=False), now_iso(), expires_at or None))
        return token

    def report(self, token: str):
        row = self.db.one("SELECT * FROM fin_reports WHERE token=?", (token,))
        if row is None or row["revoked_at"]:
            return None
        if row["expires_at"] and row["expires_at"] < date.today().isoformat():
            return None
        return row

    def reports(self):
        return self.db.q("SELECT * FROM fin_reports WHERE revoked_at IS NULL ORDER BY created_at DESC")

    def revoke_report(self, token: str) -> None:
        self.db.q("UPDATE fin_reports SET revoked_at=? WHERE token=?", (now_iso(), token))
