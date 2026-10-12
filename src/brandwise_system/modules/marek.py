"""Podsumowanie pracy Marka – z lokalnego /_summary panelu brandwise-agent (ten sam serwer)."""

from __future__ import annotations

import httpx

from . import Module, Row, Tile, pln


class Marek(Module):
    key = "marek"
    title = "Marek"

    def __init__(self, url: str, host: str):
        self.url, self.host = url, host

    async def fetch(self) -> dict:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(self.url, headers={"Host": self.host})
            r.raise_for_status()
            return r.json()

    async def summary(self) -> Tile:
        d = await self.fetch()
        st = d.get("states", {})
        base = d.get("url", f"https://{self.host}/")
        t = Tile(key=self.key, title=self.title, href=base, href_label="Panel Marka")
        mine = st.get("czeka na Ciebie", 0)
        t.stats = [
            Row("czeka na Ciebie", str(mine), tone="alert" if mine else ""),
            Row("w trakcie", str(st.get("w trakcie", 0))),
            Row("czeka na klienta", str(st.get("czeka na klienta", 0))),
            Row("otwarte oferty", pln(d.get("open_offers_net")), hint="netto"),
        ]
        if d.get("stopped"):
            t.stats.insert(0, Row("status", "ZATRZYMANY", tone="alert"))
        t.rows_title = "Czeka na Ciebie"
        t.rows = [Row(p.get("name") or "—", p.get("stage", ""), hint=p.get("client", ""),
                      href=f"{base.rstrip('/')}/p/{p['id']}") for p in d.get("waiting_for_owner", [])]
        return t
