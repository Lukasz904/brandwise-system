"""Widoki panelu finansowego (zlecenia, klienci, koszty, podsumowania, raporty dla klientów)."""

from __future__ import annotations

import io
import json
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .finance import Filters, Finance, month_label, prev_month, to_grosze, to_qty, zl

router = APIRouter()


def fin(request: Request) -> Finance:
    return request.app.state.finance


def page(request: Request, name: str, **ctx):
    from .app import page_response
    return page_response(request, name, **ctx)


def back(url: str, msg: str = "") -> RedirectResponse:
    if not url.startswith("/") or url.startswith("//") or "\\" in url:
        url = "/zlecenia"                               # tylko adresy wewnątrz systemu
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (f"{sep}ok={msg}" if msg else ""), status_code=303)


def this_month() -> str:
    return date.today().strftime("%Y-%m")


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
                clients=F.clients(), statuses=F.get_list("status"), inv_statuses=F.get_list("invoice_status"),
                query=urlencode(f.to_query(), doseq=True), msg=request.query_params.get("ok", ""))


def order_form(request: Request, order=None, duplicate=False):
    F = fin(request)
    data = dict(order) if order else {"day": date.today().isoformat(), "qty": 1, "costs": "[]",
                                      "status": (F.get_list("status") or [""])[0], "invoice_status": "Brak"}
    data["costs_list"] = json.loads(data.get("costs") or "[]")
    if duplicate:
        data["id"] = None
        data["day"] = date.today().isoformat()
    return page(request, "fin_zlecenie.html", o=data, clients=F.clients(), statuses=F.get_list("status"),
                inv_statuses=F.get_list("invoice_status"), duplicate=duplicate, error="")


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
    data = form_order(form)
    F = fin(request)
    try:
        new_id = F.save_order(data, oid)
    except ValueError as e:
        data["costs_list"] = data["costs"]
        data["price"] = data["price"]
        data["id"] = oid
        return page(request, "fin_zlecenie.html", o=data, clients=F.clients(), statuses=F.get_list("status"),
                    inv_statuses=F.get_list("invoice_status"), duplicate=False, error=str(e))
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
    month = m or this_month()
    F = fin(request)
    rows = F.costs(month)
    return page(request, "fin_koszty.html", month=month, label=month_label(month), rows=rows,
                total=sum(r["value"] for r in rows), months=F.cost_months(), prev=prev_month(month),
                msg=request.query_params.get("ok", ""))


@router.post("/koszty")
async def cost_add(request: Request):
    form = await request.form()
    month = form.get("month", this_month())
    try:
        fin(request).add_cost(month, form.get("name", ""), to_grosze(form.get("value")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return back(f"/koszty?m={month}", "dodano")


@router.post("/koszty/kopiuj")
async def cost_copy(request: Request):
    form = await request.form()
    n = fin(request).copy_costs(form.get("src", ""), form.get("dst", ""))
    return back(f"/koszty?m={form.get('dst')}", f"skopiowano-{n}")


@router.post("/koszty/{cid}")
async def cost_update(request: Request, cid: int):
    form = await request.form()
    fin(request).update_cost(cid, form.get("name", ""), to_grosze(form.get("value")))
    return back(f"/koszty?m={form.get('month', this_month())}", "zapisano")


@router.post("/koszty/{cid}/usun")
async def cost_delete(request: Request, cid: int):
    form = await request.form()
    fin(request).delete_cost(cid)
    return back(f"/koszty?m={form.get('month', this_month())}", "usunieto")


# ---------- podsumowania ----------
@router.get("/finanse", response_class=HTMLResponse)
async def summary(request: Request, m: str = ""):
    F = fin(request)
    month = m or this_month()
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
    f = Filters.from_query(json.loads(form.get("filters") or "{}"))
    token = fin(request).create_report(f, form.get("title", ""), form.get("expires") or None)
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
    ws.append(["Data", "Nr", "Tytuł", "Klient", "Zleceniodawca", "Status", "Cena netto", "Ilość", "Wartość netto"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for o in orders:
        ws.append([o["day"], o["hashtag"], o["title"], o["client_name"], o["principal"], o["status"],
                   o["price"] / 100, o["qty"], o["total"] / 100])
    ws.append([])
    ws.append(["", "", "Razem", "", "", "", "", "", totals["total"] / 100])
    ws[ws.max_row][8].font = Font(bold=True)
    for col, w in zip("ABCDEFGHI", (12, 12, 50, 28, 24, 16, 14, 8, 16)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2, min_col=7, max_col=9):
        for c in row:
            if isinstance(c.value, (int, float)):
                c.number_format = '# ##0.00'
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="Podsumowanie_brandwise.xlsx"'})


__all__ = ["router", "zl"]
