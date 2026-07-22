#!/bin/bash
# Einmalig auf dem Hetzner-Server ausführen als root
# Führt folgende Schritte durch:
#   1. Repo klonen
#   2. Python-Venv + Dependencies
#   3. .env anlegen (interaktiv)
#   4. systemd-Service einrichten
#   5. nginx-Vhost einrichten
#   6. SSL via certbot

set -e

REPO="https://github.com/sebthamm/branchenradar.git"
APP_DIR="/opt/branchenradar"
SERVICE="branchenradar"
PORT=5001
DOMAIN=""

echo ""
echo "=== Branchenradar Server Setup ==="
echo ""
read -p "Domain (z.B. radar.sebastianthamm.de): " DOMAIN
read -p "Admin-Benutzername [admin]: " ADMIN_USER
ADMIN_USER="${ADMIN_USER:-admin}"
read -s -p "Admin-Passwort: " ADMIN_PASS
echo ""
ADMIN_HASH=$(python3 -c "import hashlib; print(hashlib.sha256(b'${ADMIN_PASS}').hexdigest())")
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
DEPLOY_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(24))")

echo ""
echo "--- Pakete installieren ---"
apt-get update -q
apt-get install -y -q python3-pip python3-venv git nginx certbot python3-certbot-nginx

echo "--- Repo klonen nach $APP_DIR ---"
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR" && git pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi

echo "--- Python-Umgebung einrichten ---"
cd "$APP_DIR"
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

echo "--- .env anlegen ---"
cat > "$APP_DIR/.env" << ENVEOF
SECRET_KEY=${SECRET_KEY}
ADMIN_USER=${ADMIN_USER}
ADMIN_PASS_HASH=${ADMIN_HASH}
DEPLOY_TOKEN=${DEPLOY_TOKEN}
ENVEOF
chmod 600 "$APP_DIR/.env"

chown -R www-data:www-data "$APP_DIR"
chmod -R 755 "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "--- systemd Service ---"
cat > "/etc/systemd/system/${SERVICE}.service" << SVCEOF
[Unit]
Description=Branchenradar / Praxis-Radar
After=network.target

[Service]
User=www-data
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/gunicorn -w 2 -b 127.0.0.1:${PORT} app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 2
systemctl is-active "$SERVICE" && echo "✓ Service läuft" || echo "✗ Service-Fehler — prüfe: journalctl -u $SERVICE -n 30"

echo "--- nginx Vhost ---"
cat > "/etc/nginx/sites-available/${SERVICE}" << NGINXEOF
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 60;
    }
}
NGINXEOF

ln -sf "/etc/nginx/sites-available/${SERVICE}" "/etc/nginx/sites-enabled/"
nginx -t && systemctl reload nginx

echo "--- SSL via certbot ---"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m mail@sebastianthamm.de --redirect

echo ""
echo "========================================="
echo "✓ Setup abgeschlossen!"
echo ""
echo "  URL:          https://${DOMAIN}"
echo "  Admin-Login:  https://${DOMAIN}/login"
echo "  Deploy-Token: ${DEPLOY_TOKEN}"
echo ""
echo "  Diesen Deploy-Token brauchst du für:"
echo "  curl -X POST https://${DOMAIN}/deploy?token=${DEPLOY_TOKEN}"
echo "========================================="
