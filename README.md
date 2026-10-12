# brandwise-system

System firmowy Brandwise pod `https://panel.brandwise.pl`: dashboard (Marek, faktury, finanse, linki),
nieopłacone faktury z Fakturowni z oznaczaniem opłaconych, logowanie kluczem dostępu (Touch ID / Face ID).

## Instalacja na VPS (jednorazowo)

**1. DNS.** W panelu DevilWEB (mydevil), strefa `brandwise.pl`: rekord **A** dla `panel` → adres IP VPS-a.
Dziś pod `panel.brandwise.pl` stoi stary panel finansowy – przełącz DNS dopiero po migracji albo na czas
przejściowy użyj innej nazwy (`host` w `config.toml` i `deploy/brandwise-system.caddy`, przez PR).

**2. Instalacja.** Na Macu w Terminalu: `ssh ubuntu@<IP VPS-a>`. Potem na serwerze (prompt `ubuntu@…:~$`):
```bash
git clone https://github.com/Lukasz904/brandwise-system.git /tmp/bws && bash /tmp/bws/deploy/install.sh
```
`git clone` zapyta o login GitHub (repo prywatne): login + token z https://github.com/settings/tokens zamiast hasła.
Skrypt pokaże klucz wdrożeniowy – dodaj go w GitHub (repo → Settings → Deploy keys → Add deploy key,
bez „Allow write access”) i naciśnij Enter.

**3. Token Fakturowni.** Na serwerze:
```bash
sudo -u brandwise nano /opt/brandwise-system/.env
```
Wpisz `FAKTUROWNIA_API_TOKEN=…` (Fakturownia → Ustawienia → Ustawienia konta → Integracja → kod API), zapisz
(Ctrl+O, Enter, Ctrl+X), potem `sudo systemctl restart brandwise-system`.
Test, tylko odczyt:
```bash
cd /opt/brandwise-system && sudo -u brandwise .venv/bin/python -m brandwise_system faktury-test
```

**4. Klucz dostępu.** Na serwerze:
```bash
cd /opt/brandwise-system && sudo -u brandwise .venv/bin/python -m brandwise_system zaproszenie lukasz@brandwise.pl
```
Otwórz wypisany link w Safari na Macu → „Utwórz klucz” → Touch ID. iCloud przeniesie klucz na iPhone'a.

## Aktualizacje
Timer `brandwise-system-update` co 5 minut pobiera `main`, uruchamia testy i dopiero wtedy restartuje usługę.

## Moduły
- **Marek** – lokalne `http://127.0.0.1:8780/_summary` z brandwise-agent.
- **Faktury** – Fakturownia API: VAT, zaliczkowe, końcowe (`kinds` w `config.toml`), bufor 5 min.
- **Panel finansowy** – na razie link do obecnego panelu; podsumowanie po migracji.
