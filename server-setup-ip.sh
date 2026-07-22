#!/bin/bash
# Server-Setup OHNE Domain (IP-only, kein SSL)
# Aufruf: bash server-setup-ip.sh
set -e

REPO="https://github.com/sebthamm/branchenradar.git"
APP_DIR="/opt/branchenradar"
SERVICE="branchenradar"
PORT=5001

echo ""
echo "=== Branchenradar Setup (IP-only) ==="
echo ""
read -p "Admin-Benutzername [admin]: " ADMIN_USER
ADMIN_USER="${ADMIN_USER:-admin}"
read -s -p "Admin-Passwort: " ADMIN_PASS
echo ""
ADMIN_HASH=$(python3 -c "import hashlib; print(hashlib.sha256(b'${ADMIN_PASS}').hexdigest())")
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
DEPLOY_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(24))")

echo "--- Pakete installieren ---"
apt-get update -q
apt-get install -y -q python3-pip python3-venv git nginx

echo "--- Repo klonen nach $APP_DIR ---"
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR" && git pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi

echo "--- Python-Umgebung ---"
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

echo "--- systemd Service ---"
cat > "/etc/systemd/system/${SERVICE}.service" << SVCEOF
[Unit]
Description=Branchenradar
After=network.target

[Service]
User=www-data
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/gunicorn -w 2 -b 127.0.0.1:${PORT} app:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 2
systemctl is-active "$SERVICE" && echo "✓ Service läuft" || (echo "✗ Fehler:" && journalctl -u "$SERVICE" -n 20 --no-pager)

echo "--- nginx (Port 80) ---"
cat > "/etc/nginx/sites-available/${SERVICE}" << NGINXEOF
server {
    listen 80 default_server;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
NGINXEOF

# Alte default-Site deaktivieren falls vorhanden
rm -f /etc/nginx/sites-enabled/default
ln -sf "/etc/nginx/sites-available/${SERVICE}" "/etc/nginx/sites-enabled/"
nginx -t && systemctl reload nginx

echo ""
echo "========================================="
echo "✓ Fertig!"
echo ""
echo "  App:          http://178.105.170.27"
echo "  Admin-Login:  http://178.105.170.27/login"
echo "  Deploy-Token: ${DEPLOY_TOKEN}"
echo ""
echo "  Notiere den Deploy-Token!"
echo "========================================="
