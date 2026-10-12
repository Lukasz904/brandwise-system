"""Widoki panelu finansowego (zlecenia, klienci, koszty, podsumowania, raporty dla klientów)."""

from __future__ import annotations

import io
import json
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .finance import Filters, Finance, month_label, prev_month, to_grosze, to_qty, valid_month, zl

router = APIRouter()


def fin(request: Request) -> Finance:
    return request.app.state.finance


def page(request: Request, name: str, **ctx):
    from .app import page_response
    return page_response(request, name, **ctx)


def safe_path(url: str | None, default: str = "/zlecenia") -> str:
    url = url or ""
    if not url.startswith("/") or url.startswith("//") or "\\" in url:
        return default                                  # tylko adresy wewnątrz systemu
    return url


def back(url: str, msg: str = "") -> RedirectResponse:
    url = safe_path(url)
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (f"{sep}ok={msg}" if msg else ""), status_code=303)


def this_month() -> str:
    return date.today().strftime("%Y-%m")


def month_or_now(m: str) -> str:
    return m if valid_month(m) else this_month()


def client_choices(F: Finance, *keep) -> list:
    """Aktywni klienci + usunięci, do których odwołuje się edytowane zlecenie (żeby nie zgubić płatnika)."""
    active = list(F.clients())
    ids = {c["id"] for c in active}
    extra = [{"id": c["id"], "name": c["name"] + " (usunięty)"} for c in F.clients(with_deleted=True)
             if c["id"] in keep and c["id"] not in ids]
    return active + extra


def filter_choices(F: Finance) -> list:
    return [{"id": c["id"], "name": c["name"] + (" (usunięty)" if c["deleted_at"] else "")}
            for c in F.clients(with_deleted=True)]


def form_order(form) -> dict:
    names, values = form.getlist("koszt_nazwa"), form.getlist("koszt_kwota")
    return {
        "hashtag": form.get("hashtag", ""), "client_id": form.get("client_id") or 0,
        "payer_id": form.get("payer_id") or form.get("client_id") or 0, "day": form.get("day", ""),
        "title": form.get("title", ""), "status": form.get("status", ""), "principal": form.get("principal", ""),
        "price": to_grosze(form.get("price")), "qty": to_qty(form.get("qty")),
        "invoice_status": form.get("invoice_status", ""),
        "costs": [[n, to_grosze(v)] for n, v in zip(names, values)],
    }


def log(request: Request, event: str, detail: str = "") -> None:
    from .auth import current_user
    u = current_user(request)
    request.app.state.db.log(event, u["email"] if u else None, detail)


# ---------- zlecenia ----------
@router.get("/zlecenia", response_class=HTMLResponse)
async def orders_list(request: Request):
    f = Filters.from_query(request.query_params)
    if f.empty and "wszystkie" not in request.query_params:
        f.day_from = f"{this_month()}-01"
    F = fin(request)
    limit = 2000
    return page(request, "fin_zlecenia.html", f=f, orders=F.orders(f, limit), totals=F.totals(f), limit=limit,
                clients=filter_choices(F), statuses=F.get_list("status"), inv_statuses=F.get_list("invoice_status"),
                query=urlencode(f.to_query(), doseq=True), msg=request.query_params.get("ok", ""))


def order_form(request: Request, order=None, duplicate=False):
    F = fin(request)
    data = dict(order) if order else {"day": date.today().isoformat(), "qty": 1, "costs": "[]",
                                      "status": (F.get_list("status") or [""])[0], "invoice_status": "Brak"}
    data["costs_list"] = json.loads(data.get("costs") or "[]")
    if duplicate:
        data["id"] = None
        data["day"] = date.today().isoformat()
    return page(request, "fin_zlecenie.html", o=data, clients=client_choices(F, data.get("client_id"), data.get("payer_id")),
                statuses=F.get_list("status"), inv_statuses=F.get_list("invoice_status"), duplicate=duplicate,
                error="", wroc=safe_path(request.query_params.get("wroc")))


@router.get("/zlecenia/nowe", response_class=HTMLResponse)
async def order_new(request: Request):
    return order_form(request)


@router.get("/zlecenia/{oid}", response_class=HTMLResponse)
async def order_edit(request: Request, oid: int):
    o = fin(request).order(oid)
    if o is None:
        raise HTTPException(404, "Nie ma takiego zlecenia.")
    return order_form(request, o)


@router.get("/zlecenia/{oid}/duplikuj", response_class=HTMLResponse)
async def order_duplicate(request: Request, oid: int):
    o = fin(request).order(oid)
    if o is None:
        raise HTTPException(404, "Nie ma takiego zlecenia.")
    return order_form(request, o, duplicate=True)


async def _save(request: Request, oid: int | None):
    form = await request.form()
    F = fin(request)
    try:
        data = form_order(form)
        new_id = F.save_order(data, oid)
    except (ValueError, OverflowError) as e:
        data = {k: form.get(k, "") for k in ("hashtag", "day", "title", "status", "principal", "invoice_status")}
        data.update(id=oid, client_id=int(form.get("client_id") or 0), payer_id=int(form.get("payer_id") or 0),
                    price=0, qty=form.get("qty") or 1,
                    costs_list=[])
        return page(request, "fin_zlecenie.html", o=data, clients=client_choices(F, data["client_id"], data["payer_id"]),
                    statuses=F.get_list("status"), inv_statuses=F.get_list("invoice_status"), duplicate=False,
                    error=str(e) + " Popraw dane i zapisz ponownie (cena i koszty wymagają ponownego wpisania).",
                    wroc=safe_path(form.get("wroc")))
    log(request, "order_saved" if oid else "order_created", str(new_id))
    return back(form.get("wroc") or "/zlecenia", "zapisano")


@router.post("/zlecenia")
async def order_create(request: Request):
    return await _save(request, None)


@router.post("/zlecenia/zbiorczo")
async def orders_bulk(request: Request):
    form = await request.form()
    ids = [int(x) for x in form.getlist("id") if x.isdigit()]
    try:
        n = fin(request).bulk_update(ids, form.get("status", ""), form.get("invoice_status", ""), form.get("day", ""))
    except ValueError as e:
        raise HTTPException(400, "Niepoprawna data.") from e
    log(request, "orders_bulk", f"{n}: {','.join(map(str, ids))[:200]}")
    return back(form.get("wroc") or "/zlecenia", f"zmieniono-{n}")


@router.post("/zlecenia/{oid}")
async def order_update(request: Request, oid: int):
    if fin(request).order(oid) is None:
        raise HTTPException(404, "Nie ma takiego zlecenia.")
    return await _save(request, oid)


@router.post("/zlecenia/{oid}/usun")
async def order_delete(request: Request, oid: int):
    form = await request.form()
    fin(request).delete_order(oid)
    log(request, "order_deleted", str(oid))
    return back(form.get("wroc") or "/zlecenia", "usunieto")


# ---------- klienci ----------
@router.get("/klienci", response_class=HTMLResponse)
async def clients(request: Request):
    return page(request, "fin_klienci.html", clients=fin(request).clients(), msg=request.query_params.get("ok", ""),
                error=request.query_params.get("blad", ""))


@router.post("/klienci")
async def client_add(request: Request):
    form = await request.form()
    try:
        fin(request).add_client(form.get("name", ""))
    except ValueError as e:
        return back(f"/klienci?blad={e}")
    return back("/klienci", "dodano")


@router.post("/klienci/{cid}")
async def client_rename(request: Request, cid: int):
    form = await request.form()
    try:
        fin(request).rename_client(cid, form.get("name", ""))
    except ValueError as e:
        return back(f"/klienci?blad={e}")
    return back("/klienci", "zapisano")


@router.post("/klienci/{cid}/usun")
async def client_delete(request: Request, cid: int):
    fin(request).delete_client(cid)
    log(request, "client_deleted", str(cid))
    return back("/klienci", "usunieto")


# ---------- koszty miesięczne ----------
@router.get("/koszty", response_class=HTMLResponse)
async def costs(request: Request, m: str = ""):
    month = month_or_now(m)
    F = fin(request)
    rows = F.costs(month)
    return page(request, "fin_koszty.html", month=month, label=month_label(month), rows=rows,
                total=sum(r["value"] for r in rows), months=F.cost_months(), prev=prev_month(month),
                msg=request.query_params.get("ok", ""))


@router.post("/koszty")
async def cost_add(request: Request):
    form = await request.form()
    month = month_or_now(form.get("month", ""))
    try:
        fin(request).add_cost(month, form.get("name", ""), to_grosze(form.get("value")))
    except (ValueError, OverflowError) as e:
        raise HTTPException(400, str(e)) from e
    return back(f"/koszty?m={month}", "dodano")


@router.post("/koszty/kopiuj")
async def cost_copy(request: Request):
    form = await request.form()
    if not (valid_month(form.get("src", "")) and valid_month(form.get("dst", ""))):
        raise HTTPException(400, "Niepoprawny miesiąc.")
    n = fin(request).copy_costs(form.get("src", ""), form.get("dst", ""))
    return back(f"/koszty?m={form.get('dst')}", f"skopiowano-{n}")


@router.post("/koszty/{cid}")
async def cost_update(request: Request, cid: int):
    form = await request.form()
    try:
        fin(request).update_cost(cid, form.get("name", ""), to_grosze(form.get("value")))
    except (ValueError, OverflowError) as e:
        raise HTTPException(400, str(e)) from e
    return back(f"/koszty?m={month_or_now(form.get('month', ''))}", "zapisano")


@router.post("/koszty/{cid}/usun")
async def cost_delete(request: Request, cid: int):
    form = await request.form()
    fin(request).delete_cost(cid)
    return back(f"/koszty?m={month_or_now(form.get('month', ''))}", "usunieto")


# ---------- podsumowania ----------
@router.get("/finanse", response_class=HTMLResponse)
async def summary(request: Request, m: str = ""):
    F = fin(request)
    month = month_or_now(m)
    return page(request, "fin_finanse.html", cur=F.month_summary(month), prev=F.month_summary(prev_month(month)),
                by_client=F.by_client(month), history=F.history(), years=F.year_table(), month=month,
                prev_m=prev_month(month))


# ---------- ustawienia statusów ----------
@router.get("/ustawienia", response_class=HTMLResponse)
async def settings_page(request: Request):
    F = fin(request)
    return page(request, "fin_ustawienia.html", statuses="\n".join(F.get_list("status")),
                inv_statuses="\n".join(F.get_list("invoice_status")), msg=request.query_params.get("ok", ""))


@router.post("/ustawienia")
async def settings_save(request: Request):
    form = await request.form()
    F = fin(request)
    F.set_list("status", form.get("status", "").splitlines())
    F.set_list("invoice_status", form.get("invoice_status", "").splitlines())
    return back("/ustawienia", "zapisano")


# ---------- raporty dla klientów ----------
@router.get("/raporty", response_class=HTMLResponse)
async def reports(request: Request):
    F = fin(request)
    names = F.client_names()
    rows = [{"r": r, "f": Filters.from_query(json.loads(r["filters"]))} for r in F.reports()]
    return page(request, "fin_raporty.html", rows=rows, names=names, origin=request.app.state.cfg.origin,
                msg=request.query_params.get("ok", ""), new=request.query_params.get("nowy", ""))


@router.post("/raporty")
async def report_create(request: Request):
    form = await request.form()
    try:
        raw = json.loads(form.get("filters") or "{}")
        if not isinstance(raw, dict):
            raise ValueError("Niepoprawne filtry.")
        token = fin(request).create_report(Filters.from_query(raw), form.get("title", ""), form.get("expires") or None)
    except ValueError as e:
        msg = str(e) if "klienta" in str(e) else "Niepoprawne filtry albo data ważności."
        return RedirectResponse("/zlecenia?" + urlencode({**raw, "blad": msg}, doseq=True) if isinstance(raw, dict)
                                else "/zlecenia", status_code=303)
    log(request, "report_created", token[:8])
    return RedirectResponse(f"/raporty?nowy={token}", status_code=303)


@router.post("/raporty/{token}/wylacz")
async def report_revoke(request: Request, token: str):
    fin(request).revoke_report(token)
    log(request, "report_revoked", token[:8])
    return back("/raporty", "wylaczono")


def _report_data(request: Request, token: str):
    F = fin(request)
    r = F.report(token)
    if r is None:
        raise HTTPException(404, "Link wygasł albo jest nieaktywny.")
    f = Filters.from_query(json.loads(r["filters"]))
    return r, f, F.orders(f, limit=None), F.totals(f)


@router.get("/r/{token}", response_class=HTMLResponse)
async def report_public(request: Request, token: str):
    r, f, orders, totals = _report_data(request, token)
    return page(request, "fin_raport_publiczny.html", public=True, r=r, f=f, orders=orders, totals=totals, token=token)


@router.get("/r/{token}/xlsx")
async def report_xlsx(request: Request, token: str):
    r, f, orders, totals = _report_data(request, token)
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Podsumowanie"
    ws.append(["Data", "Nr", "Tytuł", "Zleceniodawca", "Status", "Cena netto", "Ilość", "Wartość netto"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for o in orders:
        ws.append([o["day"], o["hashtag"], o["title"], o["principal"], o["status"],
                   o["price"] / 100, o["qty"], o["total"] / 100])
    ws.append([])
    ws.append(["", "", "Razem", "", "", "", "", totals["total"] / 100])
    ws[ws.max_row][7].font = Font(bold=True)
    for col, w in zip("ABCDEFGH", (12, 12, 50, 24, 16, 14, 8, 16)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2, min_col=6, max_col=8):
        for c in row:
            if isinstance(c.value, (int, float)):
                c.number_format = '# ##0.00'
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="Podsumowanie_brandwise.xlsx"'})


__all__ = ["router", "zl"]
