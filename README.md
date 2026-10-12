# brandwise-system

System firmowy Brandwise pod `https://system.brandwise.cloud` (przejściowo): dashboard (Marek, faktury, finanse, linki),
nieopłacone faktury z Fakturowni z oznaczaniem opłaconych, logowanie kluczem dostępu (Touch ID / Face ID).

## Instalacja na VPS (jednorazowo)

Adres przejściowy: `https://system.brandwise.cloud` (docelowo `panel.brandwise.pl`; po zmianie adresu
klucze dostępu trzeba utworzyć ponownie – są przypisane do domeny).

**1. DNS.** W strefie `brandwise.cloud`: rekord **A** `system` → `149.56.103.99` (VPS, ten sam co panel Marka).
Dziś `*.brandwise.cloud` wskazuje na 212.91.26.80, więc bez tego rekordu adres trafi nie tam, gdzie trzeba.

**2. Klucz wdrożeniowy.** Na Macu w Terminalu: `ssh ubuntu@149.56.103.99`. Na serwerze (prompt `ubuntu@…:~$`):
```bash
sudo useradd --system --create-home --home-dir /var/lib/brandwise --shell /usr/sbin/nologin brandwise; sudo -u brandwise mkdir -p -m 700 /var/lib/brandwise/.ssh && sudo -u brandwise ssh-keygen -q -t ed25519 -N "" -C brandwise-system@vps -f /var/lib/brandwise/.ssh/id_ed25519 && sudo cat /var/lib/brandwise/.ssh/id_ed25519.pub
```
Skopiuj wypisaną linię (`ssh-ed25519 …`) i dodaj w GitHub: repo brandwise-system → Settings → Deploy keys →
Add deploy key (bez „Allow write access”).

**3. Instalacja.** Dalej na serwerze:
```bash
sudo install -d -o brandwise -g brandwise /opt/brandwise-system && sudo -u brandwise sh -c 'ssh-keyscan -t ed25519 github.com >> /var/lib/brandwise/.ssh/known_hosts 2>/dev/null; git clone -q git@github.com:Lukasz904/brandwise-system.git /opt/brandwise-system' && bash /opt/brandwise-system/deploy/install.sh
```

**4. Token Fakturowni.** Na serwerze:
```bash
sudo -u brandwise nano /opt/brandwise-system/.env
```
Wpisz `FAKTUROWNIA_API_TOKEN=…` (Fakturownia → Ustawienia → Ustawienia konta → Integracja → Kod autoryzacyjny API, w całości), zapisz
(Ctrl+O, Enter, Ctrl+X), potem `sudo systemctl restart brandwise-system`.
Test, tylko odczyt:
```bash
cd /opt/brandwise-system && sudo -u brandwise .venv/bin/python -m brandwise_system faktury-test
```

**5. Klucz dostępu.** Na serwerze:
```bash
cd /opt/brandwise-system && sudo -u brandwise .venv/bin/python -m brandwise_system zaproszenie lukasz@brandwise.pl
```
Otwórz wypisany link w Safari na Macu → „Utwórz klucz” → Touch ID. iCloud przeniesie klucz na iPhone'a.

## Przeniesienie danych ze starego panelu finansowego

Dane idą z serwera na serwer – nie przez czat. Najnowszy zrzut robi się sam w nocy 1–3 dnia miesiąca;
na moment przełączenia warto zrobić świeży.

1. W menedżerze plików na s23 (https://panel23.mydevil.net/file_manager/) wejdź w
   `domains/panel.brandwise.pl/public_html/public/db_backup/backups`, zaznacz najnowszy plik `.sql` → **Ściągnij**.
2. Na Macu, w Terminalu (podmień nazwę pliku):
   ```bash
   scp ~/Downloads/db_backup-02-10-2026_11-00-00.sql ubuntu@149.56.103.99:/tmp/panel.sql
   ```
3. Na serwerze (`ssh ubuntu@149.56.103.99`):
   ```bash
   sudo install -o brandwise -g brandwise -m 600 /tmp/panel.sql /opt/brandwise-system/data/panel.sql && rm /tmp/panel.sql && cd /opt/brandwise-system && sudo -u brandwise .venv/bin/python -m brandwise_system import-panel data/panel.sql && sudo -u brandwise rm data/panel.sql
   ```
   Na końcu wypisze kontrolę: liczbę zleceń i sumę netto w każdym roku – porównaj ze starym panelem.
   Ponowny import (np. świeższego zrzutu) – dopisz `--zastap`.

## Aktualizacje
Timer `brandwise-system-update` co 5 minut pobiera `main`, uruchamia testy i dopiero wtedy restartuje usługę.

## Moduły
- **Marek** – lokalne `http://127.0.0.1:8780/_summary` z brandwise-agent.
- **Faktury** – Fakturownia API: VAT, zaliczkowe, końcowe (`kinds` w `config.toml`), bufor 5 min.
- **Zlecenia / Finanse / Koszty / Klienci** – panel finansowy przepisany ze starego panelu (Laravel na s23).
  Konta użytkowników nie są przenoszone; nazwa accounta zostaje w starych zleceniach jako opis.
- **Raporty** – linki dla klientów z losowym tokenem (`/r/…`), z datą ważności i wyłączaniem; XLSX.
