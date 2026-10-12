# brandwise-system – zasady projektu

System firmowy Brandwise: jeden adres, jedno logowanie (passkeys), dashboard z modułami
(Marek, Fakturownia, panel finansowy) i linki. Działa na VPS obok Marka, jako osobny użytkownik `brandwise`.

## Zasady
1. Zmiany wyłącznie przez gałąź i PR; Claude scala sam po zielonych testach i informuje Łukasza jednym zdaniem.
   `main` jest automatycznie wdrażany na serwer (timer co 5 min; restart tylko gdy testy przechodzą).
2. Żadnych sekretów w repo – tylko `.env` (poza repo) i `.env.example` z nazwami.
3. Marek (brandwise-agent) nie ma dostępu do tego systemu, jego plików ani tokenów. Od Marka czytamy wyłącznie
   lokalne `/_summary`. Nie dawaj Markowi dostępu do Fakturowni ani finansów bez zgody Łukasza.
4. Każdy moduł daje kafelek przez `Module.summary()`; awaria modułu nie może wywrócić dashboardu.
5. Zmiana w Fakturowni (np. „opłacona”) tylko na wyraźne kliknięcie człowieka; każda trafia do tabeli `audit`.
6. Logowanie wyłącznie passkeys; każdy POST sprawdza nagłówek Origin.

## Komendy
- Testy: `.venv/bin/pytest -q`
- Lokalnie: `.venv/bin/python -m brandwise_system serve`
