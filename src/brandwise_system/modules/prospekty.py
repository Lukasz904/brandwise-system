"""Kafelek prospektów: sprawy w zaawansowanych rozmowach, jeszcze niezaakceptowane."""

from __future__ import annotations

import asyncio

from ..finance import Filters, Finance, zl
from . import Module, Row, Tile


class Prospekty(Module):
    key = "prospekty"
    title = "Prospekty"

    def __init__(self, finance: Finance):
        self.finance = finance

    async def summary(self) -> Tile:
        return await asyncio.to_thread(self._summary)

    def _summary(self) -> Tile:
        f = Filters(kind="prospekt")
        t = self.finance.totals(f)
        tile = Tile(key=self.key, title=self.title, href="/prospekty", href_label="Wszystkie")
        tile.stats = [Row("w rozmowach", zl(t["total"]), hint=f"{t['n']} spraw, netto"),
                      Row("po kosztach", zl(t["net"]))]
        top = sorted(self.finance.orders(f, limit=None), key=lambda o: o["total"], reverse=True)[:5]
        tile.rows_title = "Największe" if top else ""
        tile.rows = [Row(o["title"], zl(o["total"]), hint=o["client_name"], href=f"/prospekty/{o['id']}") for o in top]
        return tile
