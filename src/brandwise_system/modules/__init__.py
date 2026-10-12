"""Moduły systemu. Każdy daje dashboardowi kafelek z podsumowaniem; awaria jednego nie kładzie reszty."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Row:
    label: str
    value: str = ""
    hint: str = ""
    tone: str = ""          # ""|alert|warn|ok
    href: str = ""


@dataclass
class Tile:
    key: str
    title: str
    ok: bool = True
    error: str = ""
    stats: list[Row] = field(default_factory=list)     # duże liczby u góry kafelka
    rows: list[Row] = field(default_factory=list)      # lista pod spodem
    rows_title: str = ""
    href: str = ""                                    # „otwórz moduł”
    href_label: str = "Otwórz"


class Module:
    key = ""
    title = ""
    timeout = 8.0

    async def summary(self) -> Tile:  # pragma: no cover – interfejs
        raise NotImplementedError

    async def safe_summary(self) -> Tile:
        try:
            return await asyncio.wait_for(self.summary(), self.timeout)
        except Exception as e:  # noqa: BLE001 – kafelek pokazuje błąd zamiast wywracać dashboard
            log.warning("moduł %s: %s", self.key, e)
            msg = "przekroczony czas odpowiedzi" if isinstance(e, TimeoutError) else str(e) or type(e).__name__
            return Tile(key=self.key, title=self.title, ok=False, error=msg)


def pln(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} zł"
