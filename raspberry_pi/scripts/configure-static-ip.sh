#!/bin/bash
# Configura IP fixo 10.0.0.1 na interface ethernet da Raspberry Pi
# (link direto com o dashboard em 10.0.0.5).
#
# Uso na Raspberry:
#   sudo bash configure-static-ip.sh
#   sudo bash configure-static-ip.sh eth0
#
# Requer NetworkManager (Raspberry Pi OS Bookworm+).

set -euo pipefail

IFACE="${1:-}"
PI_IP="10.0.0.1"
PREFIX=24
DASHBOARD_IP="10.0.0.5"

# Detecta interface ethernet se nao informada
if [ -z "${IFACE}" ]; then
    for candidate in end0 eth0; do
        if [ -d "/sys/class/net/${candidate}" ]; then
            IFACE="${candidate}"
            break
        fi
    done
fi

if [ -z "${IFACE}" ] || [ ! -d "/sys/class/net/${IFACE}" ]; then
    echo "Interface ethernet nao encontrada. Uso: sudo bash $0 eth0"
    ip link show
    exit 1
fi

configure_dhcpcd() {
    local conf="/etc/dhcpcd.conf"
    local marker="# scca-static-${IFACE}"
    if [ ! -f "${conf}" ]; then
        echo "Arquivo ${conf} nao encontrado."
        return 1
    fi
    if grep -q "${marker}" "${conf}"; then
        echo "Entrada estatica ja existe em ${conf}"
        return 0
    fi
    sudo tee -a "${conf}" >/dev/null <<EOF

${marker}
interface ${IFACE}
static ip_address=${PI_IP}/${PREFIX}
static routers=
static domain_name_servers=
nohook wpa_supplicant
EOF
    sudo systemctl restart dhcpcd || sudo service dhcpcd restart
}

if ! command -v nmcli >/dev/null 2>&1; then
    echo "nmcli nao encontrado; usando dhcpcd em ${IFACE}..."
    configure_dhcpcd
    ip -4 addr show dev "${IFACE}"
    exit 0
fi

echo "Configurando ${IFACE} com IP estatico ${PI_IP}/${PREFIX} (sem gateway)..."
nmcli con down "${IFACE}" 2>/dev/null || true
nmcli con delete "${IFACE}-static" 2>/dev/null || true
nmcli con add type ethernet ifname "${IFACE}" con-name "${IFACE}-static" \
    ipv4.method manual ipv4.addresses "${PI_IP}/${PREFIX}" \
    ipv4.gateway "" ipv4.dns "" \
    ipv6.method ignore autoconnect yes
nmcli con up "${IFACE}-static"

echo ""
echo "Pronto. Teste: ping ${DASHBOARD_IP}"
ip -4 addr show dev "${IFACE}"
