"""Kafelek panelu finansowego: bieżący miesiąc i zlecenia do zafakturowania."""

from __future__ import annotations

import asyncio
from datetime import date

from ..finance import Filters, Finance, zl
from . import Module, Row, Tile


class Finanse(Module):
    key = "finanse"
    title = "Finanse"

    def __init__(self, finance: Finance):
        self.finance = finance

    async def summary(self) -> Tile:
        return await asyncio.to_thread(self._summary)

    def _summary(self) -> Tile:
        month = date.today().strftime("%Y-%m")
        s = self.finance.month_summary(month)
        todo = self.finance.totals(Filters(invoice_status=("Brak",)))
        t = Tile(key=self.key, title=f"Finanse · {s['label']}", href="/finanse", href_label="Podsumowanie")
        t.stats = [
            Row("przychód", zl(s["revenue"]), hint="po kosztach zleceń"),
            Row("dochód", zl(s["profit"]), hint="po kosztach stałych", tone="ok" if s["profit"] > 0 else "alert"),
            Row("do zafakturowania", zl(todo["total"]), hint=f"{todo['n']} zleceń",
                tone="warn" if todo["n"] else ""),
            Row("zleceń w miesiącu", str(s["orders"])),
        ]
        top = self.finance.by_client(month)[:3]
        t.rows_title = "Klienci w tym miesiącu" if top else ""
        t.rows = [Row(name, zl(v), href="/finanse") for name, v in top]
        return t
