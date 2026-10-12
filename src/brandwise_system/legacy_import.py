"""Import danych ze starego panelu (panel.brandwise.pl, Laravel/MySQL) ze zrzutu Shuttle_Dumper.

Zrzut: `CREATE TABLE ...;` + `INSERT INTO `t` VALUES ('a','b',NULL),\\n(...);` – wartości w kolejności kolumn.
Uruchamiane na serwerze; dane nie przechodzą przez nikogo po drodze:
    python -m brandwise_system import-panel /ścieżka/db_backup-....sql [--zastap]

Zasady przeniesienia:
- klienci z tymi samymi id (usunięci zostają jako usunięci, bo stare zlecenia się do nich odwołują),
- zlecenia: data z uniksowego znacznika (czas polski) → RRRR-MM-DD, kwoty w groszach, wartość z pola `sum`,
  koszty zlecenia z JSON-a {nazwa: kwota}, hashtag „0” → pusty,
- konta użytkowników nie są przenoszone; nazwa accounta zostaje w zleceniu jako opis archiwalny,
- koszty miesięczne: miesiąc z tabeli `dates` (MM/RRRR) → RRRR-MM,
- listy statusów z tabeli `settings`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .finance import Finance, line_total, now_iso, to_grosze, to_qty

TZ = ZoneInfo("Europe/Warsaw")
ESCAPES = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a", "b": "\b"}


class ImportErrorPL(Exception):
    pass


def _columns(create_sql: str) -> list[str]:
    body = create_sql[create_sql.index("(") + 1:create_sql.rindex(")")]
    cols = []
    for line in body.splitlines():
        m = re.match(r"\s*`([^`]+)`\s", line)
        if m:
            cols.append(m.group(1))
    return cols


def _values(s: str, i: int) -> tuple[list[list], int]:
    """Parsuje `(…),(…)` od pozycji i do średnika poza cudzysłowem. Zwraca wiersze i pozycję po średniku."""
    rows, row, n = [], None, len(s)
    while i < n:
        ch = s[i]
        if ch == ";" and row is None:
            return rows, i + 1
        if ch == "(" and row is None:
            row = []
            i += 1
            continue
        if row is None or ch in " \t\r\n,":
            i += 1
            continue
        if ch == ")":
            rows.append(row)
            row = None
            i += 1
            continue
        if ch == "'":
            i += 1
            buf = []
            while i < n:
                c = s[i]
                if c == "\\" and i + 1 < n:
                    nxt = s[i + 1]
                    buf.append(ESCAPES.get(nxt, nxt))
                    i += 2
                    continue
                if c == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(c)
                i += 1
            row.append("".join(buf))
            continue
        m = re.compile(r"[^,)\s]+").match(s, i)
        tok = m.group(0)
        row.append(None if tok.upper() == "NULL" else tok)
        i = m.end()
    raise ImportErrorPL("Zrzut urwany w środku instrukcji INSERT.")


def parse_dump(text: str) -> dict[str, list[dict]]:
    tables: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE TABLE `(\w+)` \((.*?)\n\)[^;]*;", text, re.S):
        tables[m.group(1)] = _columns(m.group(0))
    out: dict[str, list[dict]] = {t: [] for t in tables}
    pos = 0
    pat = re.compile(r"INSERT INTO `(\w+)`(?:\s*\(([^)]*)\))?\s+VALUES\s*")
    while True:
        m = pat.search(text, pos)
        if not m:
            break
        table = m.group(1)
        cols = [c.strip(" `") for c in m.group(2).split(",")] if m.group(2) else tables.get(table)
        if not cols:
            raise ImportErrorPL(f"Brak definicji tabeli {table} w zrzucie.")
        rows, pos = _values(text, m.end())
        for r in rows:
            if len(r) != len(cols):
                raise ImportErrorPL(f"Tabela {table}: {len(r)} wartości zamiast {len(cols)}.")
            out.setdefault(table, []).append(dict(zip(cols, r)))
    if "orders" not in out or "clients" not in out:
        raise ImportErrorPL("To nie wygląda na zrzut panelu finansowego (brak tabel orders/clients).")
    return out


def legacy_day(v) -> str:
    s = str(v or "").strip()
    if "/" in s:
        return datetime.strptime(s, "%d/%m/%Y").date().isoformat()
    return datetime.fromtimestamp(int(float(s)), TZ).date().isoformat()


def legacy_costs(raw) -> list[list]:
    """{nazwa: kwota} (albo lista kwot, gdy nazwy były 0, 1, …). Niepoprawne kwoty → ValueError."""
    if not raw:
        return []
    try:
        d = json.loads(raw)
    except ValueError:
        return []
    if isinstance(d, dict):
        items = d.items()
    elif isinstance(d, list):
        items = [(str(i), v) for i, v in enumerate(d)]
    else:
        return []
    return [[str(k), to_grosze(v)] for k, v in items]


def import_legacy(finance: Finance, data: dict[str, list[dict]], replace: bool = False, fix: bool = True) -> dict:
    """fix=True: poprawia błędy liczenia starego panelu (PHP ucinał kwoty z przecinkiem lub spacją:
    koszt „288,44” liczył jako 288, cenę „1 898” jako 1, ilość „15,5” jako 15). fix=False: liczby jak w starym panelu."""
    db = finance.db
    if not replace and any(db.one(f"SELECT 1 FROM {t} LIMIT 1") for t in ("fin_orders", "fin_clients", "fin_costs")):
        raise ImportErrorPL("W nowym systemie są już zlecenia, klienci albo koszty. Użyj --zastap, żeby zastąpić je "
                            "danymi ze zrzutu.")
    users = {str(u["id"]): u.get("name") or u.get("email") or "" for u in data.get("users", [])}
    ts = now_iso()
    stats = {"klienci": 0, "zlecenia": 0, "koszty": 0, "pominiete": [], "korekty": []}
    with db.lock:
        c = db.conn
        c.execute("BEGIN")
        try:
            if replace:
                for t in ("fin_orders", "fin_clients", "fin_costs"):
                    c.execute(f"DELETE FROM {t}")
            for r in data["clients"]:
                c.execute("INSERT INTO fin_clients(id, name, deleted_at) VALUES (?,?,?)",
                          (int(r["id"]), r["name"], r.get("deleted_at")))
                stats["klienci"] += 1
            known = {str(r["id"]) for r in data["clients"]}
            for r in data["orders"]:
                for k in ("client", "invoice"):
                    cid = str(r.get(k) or "")
                    if cid and cid not in known:
                        c.execute("INSERT INTO fin_clients(id, name, deleted_at) VALUES (?,?,?)",
                                  (int(cid), f"Klient #{cid} (brak w starym panelu)", ts))
                        known.add(cid)
                        stats["pominiete"].append(f"klient {cid}: brak w tabeli clients – dodany jako usunięty")
            for r in data["orders"]:
                try:
                    day = legacy_day(r["data"])
                    costs = legacy_costs(r.get("costs_data"))
                    listed = sum(v for _, v in costs)
                    legacy_costs_total = to_grosze(r.get("costs_sum")) if r.get("costs_sum") not in (None, "") else listed
                    costs_total = legacy_costs_total
                    if costs and listed != legacy_costs_total:
                        if fix:
                            costs_total = listed
                            stats["korekty"].append(("koszty", r["id"], legacy_costs_total, listed))
                        else:
                            stats["pominiete"].append(f"zlecenie {r['id']}: suma kosztów różni się od listy – zostawiono sumę")
                    price, qty = to_grosze(r.get("price")), to_qty(r.get("pcs"))
                    legacy_total = to_grosze(r.get("sum")) if r.get("sum") not in (None, "") else line_total(price, qty)
                    total = legacy_total
                    if abs(line_total(price, qty) - legacy_total) > 1:
                        if fix:
                            total = line_total(price, qty)
                            stats["korekty"].append(("wartosc", r["id"], legacy_total, total))
                        else:
                            stats["pominiete"].append(f"zlecenie {r['id']}: wartość różni się od cena × ilość – zostawiono wartość")
                    client, payer = int(r["client"]), int(r["invoice"] or r["client"])
                except (ValueError, OverflowError, TypeError) as e:
                    stats["pominiete"].append(f"zlecenie {r.get('id')}: POMINIĘTE ({e})")
                    continue
                hashtag = "" if str(r.get("hashtag") or "").strip() == "0" else str(r.get("hashtag") or "").strip()
                c.execute(
                    "INSERT INTO fin_orders(id, hashtag, client_id, payer_id, day, title, status, principal, price, qty,"
                    " total, costs, costs_total, invoice_status, account_legacy, created_at, updated_at, deleted_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(r["id"]), hashtag, client, payer, day, r["title"],
                     r["status"], r["principal"], price, qty, total, json.dumps(costs, ensure_ascii=False),
                     costs_total, r["invoice_status"], users.get(str(r.get("account")), ""),
                     r.get("created_at") or ts, r.get("updated_at") or ts, r.get("deleted_at")))
                stats["zlecenia"] += 1
            months = {str(d["id"]): d["value"] for d in data.get("dates", [])}
            for r in data.get("costs", []):
                mv = months.get(str(r["date_id"]))
                if not mv or "/" not in mv:
                    stats["pominiete"].append(f"koszt {r['id']}: brak miesiąca")
                    continue
                try:
                    m, y = mv.split("/")
                    month, value = f"{int(y):04d}-{int(m):02d}", to_grosze(r["value"])
                except (ValueError, OverflowError) as e:
                    stats["pominiete"].append(f"koszt {r['id']}: POMINIĘTY ({e})")
                    continue
                c.execute("INSERT INTO fin_costs(month, name, value) VALUES (?,?,?)", (month, r["name"], value))
                stats["koszty"] += 1
            for r in data.get("settings", []):
                if r["name"] in ("status", "invoice_status"):
                    try:
                        vals = json.loads(r["value"])
                    except ValueError:
                        continue
                    vals = list(vals.values()) if isinstance(vals, dict) else list(vals)   # PHP array_filter → obiekt
                    vals = [str(v).strip() for v in vals if str(v).strip()]
                    c.execute("INSERT INTO fin_settings(name, value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET "
                              "value=excluded.value", (r["name"], json.dumps(list(vals), ensure_ascii=False)))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    db.log("legacy_import", None, json.dumps({k: v if not isinstance(v, list) else len(v) for k, v in stats.items()}))
    return stats


def check_totals(finance: Finance) -> list[tuple[str, int, int, int]]:
    """Kontrola po imporcie: rok, liczba zleceń, sprzedaż netto, koszty zleceń – do porównania ze starym panelem."""
    return [(r["y"], r["n"], r["t"], r["c"]) for r in finance.db.q(
        "SELECT substr(day,1,4) y, COUNT(*) n, SUM(total) t, SUM(costs_total) c FROM fin_orders WHERE deleted_at IS NULL "
        "GROUP BY y ORDER BY y")]
