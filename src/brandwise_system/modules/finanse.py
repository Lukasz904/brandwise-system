"""Panel finansowy zleceń – do czasu migracji tylko link do obecnego panelu (panel.brandwise.pl na mydevil).

Po przeniesieniu kodu od Kuby ten moduł dostanie własne widoki i podsumowanie (zlecenia bez faktury,
suma netto do zafakturowania) oraz wspólny przepływ z Fakturownią: zlecenie → faktura → płatność.
"""

from __future__ import annotations

from . import Module, Row, Tile


class Finanse(Module):
    key = "finanse"
    title = "Panel finansowy"

    def __init__(self, url: str):
        self.url = url

    async def summary(self) -> Tile:
        return Tile(key=self.key, title=self.title, href=self.url, href_label="Obecny panel",
                    rows=[Row("Podsumowanie pojawi się po migracji panelu na ten serwer.")])
