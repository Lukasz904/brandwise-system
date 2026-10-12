#!/usr/bin/env bash
# Jednorazowa instalacja na VPS (jako ubuntu, z sudo). Instrukcja krok po kroku: README, sekcja „Instalacja”.
set -euo pipefail
REPO="git@github.com:Lukasz904/brandwise-system.git"
DIR=/opt/brandwise-system
HOME_B=/var/lib/brandwise

# 1. użytkownik systemowy bez powłoki – Marek (ubuntu) nie widzi jego plików
id brandwise >/dev/null 2>&1 || sudo useradd --system --create-home --home-dir "$HOME_B" --shell /usr/sbin/nologin brandwise
sudo chmod 700 "$HOME_B"

# 2. klucz wdrożeniowy (tylko odczyt repo)
if ! sudo test -f "$HOME_B/.ssh/id_ed25519"; then
  sudo -u brandwise mkdir -p -m 700 "$HOME_B/.ssh"
  sudo -u brandwise ssh-keygen -q -t ed25519 -N "" -C "brandwise-system@vps" -f "$HOME_B/.ssh/id_ed25519"
  sudo -u brandwise sh -c "ssh-keyscan -t ed25519 github.com >> $HOME_B/.ssh/known_hosts 2>/dev/null"
fi
if [ ! -d "$DIR/.git" ]; then
  echo
  echo "Dodaj ten klucz w GitHub: repo brandwise-system → Settings → Deploy keys → Add deploy key"
  echo "(bez zaznaczania „Allow write access”):"
  echo
  sudo cat "$HOME_B/.ssh/id_ed25519.pub"
  echo
  read -r -p "Po dodaniu klucza naciśnij Enter… " _
  sudo install -d -o brandwise -g brandwise -m 755 "$DIR"
  sudo -u brandwise git clone -q "$REPO" "$DIR"
fi

# 3. środowisko, dane i sekrety tylko dla użytkownika brandwise
sudo -u brandwise mkdir -p -m 700 "$DIR/data"
sudo -u brandwise sh -c "[ -f $DIR/.env ] || cp $DIR/.env.example $DIR/.env; chmod 600 $DIR/.env"
sudo -u brandwise python3 -m venv "$DIR/.venv"
sudo -u brandwise "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
sudo -u brandwise "$DIR/.venv/bin/pip" install -q -e "$DIR" --no-deps

# 4. usługi, automatyczne aktualizacje z main, Caddy
sudo install -d -m 0755 /etc/caddy/sites
sudo install -m 0644 "$DIR/deploy/brandwise-system.caddy" /etc/caddy/sites/
sudo install -m 0644 "$DIR/deploy/brandwise-system.service" "$DIR/deploy/brandwise-system-update.service" \
  "$DIR/deploy/brandwise-system-update.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now brandwise-system brandwise-system-update.timer
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null && sudo systemctl reload caddy
sleep 2
curl -fsS http://127.0.0.1:8790/zdrowie >/dev/null && echo "brandwise-system działa."
