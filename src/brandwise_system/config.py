"""Konfiguracja: config.toml (jawne ustawienia, w repo) + .env (sekrety, poza repo)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Link:
    group: str
    name: str
    url: str
    note: str = ""


@dataclass
class Config:
    host: str                       # domena systemu, np. panel.brandwise.pl
    origin: str                     # https://<host> – musi się zgadzać z adresem w przeglądarce (passkeys)
    data_dir: Path
    users: list[str]                # adresy, którym wolno założyć passkey (zaproszenie z CLI)
    session_days: int = 30
    marek_summary_url: str = "http://127.0.0.1:8780/_summary"
    marek_host: str = "panel.brandwise.cloud"
    fakturownia_domain: str = ""    # subdomena konta: <ta>.fakturownia.pl
    fakturownia_token: str = ""     # z .env, nigdy w repo
    fakturownia_kinds: list[str] = field(default_factory=lambda: ["vat"])
    fakturownia_cache_seconds: int = 300
    finanse_url: str = ""
    links: list[Link] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "system.db"


def load(path: str | Path | None = None, env: dict | None = None) -> Config:
    explicit_env = env is not None
    env = dict(os.environ if env is None else env)
    path = Path(path or env.get("BRANDWISE_SYSTEM_CONFIG", "config.toml"))
    dotenv = path.parent / ".env"
    if not explicit_env and dotenv.is_file():          # CLI na serwerze: sekrety z .env obok config.toml
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and not k.strip().startswith("#"):
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    host = raw["host"].strip().lower()
    fk = raw.get("fakturownia", {})
    marek = raw.get("marek", {})
    links = [Link(group=g, name=item["name"], url=item["url"], note=item.get("note", ""))
             for g, items in raw.get("links", {}).items() for item in items]
    data_dir = Path(raw.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = path.parent / data_dir
    return Config(
        host=host,
        origin=raw.get("origin", f"https://{host}"),
        data_dir=data_dir,
        users=[u.strip().lower() for u in raw.get("users", [])],
        session_days=int(raw.get("session_days", 30)),
        marek_summary_url=marek.get("summary_url", "http://127.0.0.1:8780/_summary"),
        marek_host=marek.get("host", "panel.brandwise.cloud"),
        fakturownia_domain=fk.get("domain", ""),
        fakturownia_token=env.get("FAKTUROWNIA_API_TOKEN", ""),
        fakturownia_kinds=list(fk.get("kinds", ["vat"])),
        fakturownia_cache_seconds=int(fk.get("cache_seconds", 300)),
        finanse_url=raw.get("finanse", {}).get("url", ""),
        links=links,
    )
