#!/usr/bin/env bash
set -euo pipefail

# subs-bot one-command deploy
# Usage:  ./deploy.sh
# Prereq: Python 3.11+, systemd, git

INSTALL_DIR="${INSTALL_DIR:-/opt/subs-bot}"
VENV_DIR="${INSTALL_DIR}/.venv"

echo "=== subs-bot deploy ==="
echo "  install dir: ${INSTALL_DIR}"

if [ -d "${INSTALL_DIR}/.git" ]; then
    echo ">>> git pull..."
    cd "${INSTALL_DIR}" && git pull --ff-only
else
    REPO_URL="${REPO_URL:-$(git config --get remote.origin.url || echo '')}"
    if [ -z "${REPO_URL}" ]; then
        echo "ERROR: clone this repo first or set REPO_URL env var"
        exit 1
    fi
    echo ">>> git clone ${REPO_URL} -> ${INSTALL_DIR}"
    git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"

echo ">>> creating venv..."
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip -q
"${VENV_DIR}/bin/pip" install -r requirements.txt -q

if [ ! -f "${INSTALL_DIR}/.env" ]; then
    echo ">>> copying .env.example -> .env"
    cp .env.example .env
    chmod 600 .env
    echo "  !! Edit .env: set BOT_TOKEN, ALLOWED_USER_IDS, PUBLIC_BASE_URL"
    echo "  !! Then re-run: ./deploy.sh"
    exit 0
fi

chmod 600 "${INSTALL_DIR}/.env" || true

_env_get() {
    # shellcheck disable=SC1091
    (
        set -a
        # shellcheck disable=SC1090
        . "${INSTALL_DIR}/.env"
        set +a
        eval "printf '%s' \"\${$1-}\""
    )
}

BOT_TOKEN_VALUE="$(_env_get BOT_TOKEN)"
if [ -z "${BOT_TOKEN_VALUE}" ]; then
    echo "ERROR: BOT_TOKEN is empty in ${INSTALL_DIR}/.env"
    exit 1
fi

echo ">>> installing systemd service..."
install -m 644 "${INSTALL_DIR}/subs-bot.service" /etc/systemd/system/subs-bot.service
sed -i \
    -e "s#WorkingDirectory=.*#WorkingDirectory=${INSTALL_DIR}#" \
    -e "s#EnvironmentFile=.*#EnvironmentFile=${INSTALL_DIR}/.env#" \
    -e "s#ExecStart=.*#ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/bot.py#" \
    /etc/systemd/system/subs-bot.service

systemctl daemon-reload
systemctl enable subs-bot
systemctl restart subs-bot

echo ">>> waiting for health..."
ok=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf -m 3 "http://127.0.0.1:8787/health" >/dev/null; then
        ok=1
        break
    fi
    sleep 1
done
if [ "$ok" -ne 1 ]; then
    echo "ERROR: health check failed"
    systemctl --no-pager --full status subs-bot || true
    journalctl -u subs-bot -n 40 --no-pager || true
    exit 1
fi

echo ">>> status:"
systemctl is-active subs-bot
echo "=== done ==="
echo "  service:  systemctl status subs-bot"
echo "  logs:     journalctl -u subs-bot -f"
echo "  health:   curl http://localhost:8787/health"
