import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest

from brandwise_system.modules.fakturownia import Fakturownia, FakturowniaError, amount, parse

TODAY = date(2026, 10, 12)


def inv(i, due, gross="1230.00", paid="0,00", status="issued", kind="vat", currency="PLN", buyer="Allianz"):
    return {"id": i, "number": f"{i}/10/2026", "buyer_name": buyer, "payment_to": due and due.isoformat(),
            "price_gross": gross, "price_net": "1000.00", "paid": paid, "status": status, "kind": kind,
            "currency": currency, "token": f"t{i}"}


def test_amount_formats():
    assert amount("0,00") == 0 and amount("1 230,50") == 1230.5 and amount("99.9") == 99.9 and amount(None) == 0


def test_overview():
    raw = [
        inv(1, TODAY - timedelta(days=10)),                              # po terminie
        inv(2, TODAY - timedelta(days=3), gross="500.00", paid="200.00", status="partial"),  # częściowo, po terminie
        inv(3, TODAY + timedelta(days=5)),
        inv(4, TODAY + timedelta(days=1)),
        inv(5, TODAY - timedelta(days=30), status="paid"),               # opłacona – pomijana
        inv(6, TODAY + timedelta(days=2), currency="EUR"),
        inv(7, None),
    ]
    o = Fakturownia("", "", ["vat"]).overview([parse(r) for r in raw], TODAY)
    assert [i.id for i in o["overdue"]] == [1, 2]
    assert [i.id for i in o["upcoming"]] == [4, 6, 3]
    assert [i.id for i in o["no_due"]] == [7]
    assert o["total_overdue"] == 1230 + 300
    assert o["total_open"] == 1230 + 300 + 1230 + 1230 + 1230     # bez EUR
    assert o["other_currency"] == ["EUR"]
    assert o["overdue"][0].overdue_days(TODAY) == 10


def handler_factory(pages, calls):
    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.path.endswith("/change_status.json"):
            return httpx.Response(200, json={"status": "paid"})
        page = int(request.url.params["page"])
        return httpx.Response(200, json=pages[page - 1] if page <= len(pages) else [])
    return handler


def test_pagination_and_mark_paid():
    calls = []
    pages = [[inv(i, TODAY) for i in range(1, 101)], [inv(101, TODAY), inv(102, TODAY, kind="proforma")]]
    fk = Fakturownia("brandwise", "tok", ["vat"], transport=httpx.MockTransport(handler_factory(pages, calls)))
    invs = asyncio.run(fk.invoices())
    assert len(invs) == 101                                    # proforma odfiltrowana
    assert len(calls) == 2
    q = calls[0].url
    assert q.host == "brandwise.fakturownia.pl" and q.params["api_token"] == "tok"
    assert q.params.get_list("kinds[]") == ["vat"] and q.params["period"] == "all"
    asyncio.run(fk.invoices())                                 # z bufora
    assert len(calls) == 2
    asyncio.run(fk.mark_paid(55))
    last = calls[-1]
    assert last.method == "POST" and last.url.path == "/invoices/55/change_status.json"
    assert last.url.params["status"] == "paid"
    asyncio.run(fk.invoices())                                 # bufor unieważniony po zmianie
    assert len(calls) == 5


def test_not_configured():
    with pytest.raises(FakturowniaError):
        asyncio.run(Fakturownia("", "", ["vat"]).invoices())


def test_bad_token():
    fk = Fakturownia("brandwise", "zly", ["vat"],
                     transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(FakturowniaError, match="token"):
        asyncio.run(fk.invoices())
