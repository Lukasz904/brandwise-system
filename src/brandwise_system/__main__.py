"""CLI (na serwerze):
  python -m brandwise_system serve                  – uruchomienie (127.0.0.1:8790, za Caddy)
  python -m brandwise_system zaproszenie <email>    – link do utworzenia klucza dostępu (30 min)
  python -m brandwise_system faktury-test           – sprawdzenie połączenia z Fakturownią (tylko odczyt)
  python -m brandwise_system import-panel <plik.sql> [--zastap]  – import danych ze starego panelu finansowego
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date

from .config import load


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="brandwise_system")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8790)
    z = sub.add_parser("zaproszenie")
    z.add_argument("email")
    sub.add_parser("faktury-test")
    imp = sub.add_parser("import-panel")
    imp.add_argument("plik")
    imp.add_argument("--zastap", action="store_true", help="zastąp zlecenia, klientów i koszty w nowym systemie")
    imp.add_argument("--jak-w-starym", action="store_true",
                     help="nie poprawiaj błędów liczenia starego panelu (kwoty z przecinkiem/spacją)")
    args = ap.parse_args(argv)
    cfg = load(args.config)

    if args.cmd == "serve":
        import uvicorn

        from .app import create_app
        uvicorn.run(create_app(cfg), host="127.0.0.1", port=args.port, proxy_headers=True,
                    forwarded_allow_ips="127.0.0.1", log_level="info", access_log=False)
        return 0

    if args.cmd == "zaproszenie":
        from .db import DB
        email = args.email.strip().lower()
        if email not in cfg.users:
            print(f"{email} nie ma na liście users w config.toml", file=sys.stderr)
            return 2
        token = DB(cfg.db_path).create_invite(email)
        print(f"{cfg.origin}/zaproszenie/{token}\n(ważny 30 minut, jednorazowy)")
        return 0

    if args.cmd == "faktury-test":
        from .modules.fakturownia import Fakturownia
        fk = Fakturownia(cfg.fakturownia_domain, cfg.fakturownia_token, cfg.fakturownia_kinds)
        invs = asyncio.run(fk.invoices(fresh=True))
        o = fk.overview(invs, date.today())
        print(f"Faktur: {len(invs)}, nieopłaconych: {len(o['unpaid'])}, po terminie: {len(o['overdue'])}, "
              f"do zapłaty: {o['total_open']:.2f} zł, w tym po terminie: {o['total_overdue']:.2f} zł")
        return 0
    if args.cmd == "import-panel":
        from pathlib import Path

        from .db import DB
        from .finance import Finance, zl
        from .legacy_import import ImportErrorPL, check_totals, import_legacy, parse_dump
        try:
            text = Path(args.plik).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            print("Import przerwany: plik nie jest w UTF-8 (polskie znaki by się zepsuły). Zrób zrzut w UTF-8.",
                  file=sys.stderr)
            return 2
        try:
            finance = Finance(DB(cfg.db_path))
            stats = import_legacy(finance, parse_dump(text), replace=args.zastap, fix=not args.jak_w_starym)
        except ImportErrorPL as e:
            print(f"Import przerwany: {e}", file=sys.stderr)
            return 2
        print(f"Zaimportowano: klienci {stats['klienci']}, zlecenia {stats['zlecenia']}, koszty {stats['koszty']}")
        for p in stats["pominiete"]:
            print(f"  uwaga: {p}")
        if stats["korekty"]:
            d = sum(new - old for _, _, old, new in stats["korekty"])
            print(f"Poprawione błędy starego panelu ({len(stats['korekty'])}):")
            for kind, oid, old, new in stats["korekty"]:
                print(f"  zlecenie {oid}: {'koszty' if kind == 'koszty' else 'wartość'} {zl(old)} → {zl(new)}")
        print("Kontrola – rok · zleceń · sprzedaż netto · koszty zleceń · przychód (sprzedaż − koszty):")
        for y, n, t, c in check_totals(finance):
            print(f"  {y} · {n} · {zl(t)} · {zl(c)} · {zl(t - c)}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
