#!/bin/bash
# Ajusta ethernet do PC (dashboard) para link direto com Raspberry.
# Uso: sudo bash scripts/fix-pc-ethernet.sh [interface]
# Padrao: enp2s0
set -euo pipefail

IFACE="${1:-enp2s0}"
PC_IP="10.0.0.5"
PREFIX=24

if ! command -v nmcli >/dev/null 2>&1; then
    echo "Instale NetworkManager ou configure manualmente ${IFACE} = ${PC_IP}/${PREFIX} sem gateway."
    exit 1
fi

echo "Configurando ${IFACE} = ${PC_IP}/${PREFIX} (sem gateway)..."
nmcli con down "${IFACE}" 2>/dev/null || true
nmcli con delete "${IFACE}-scca" 2>/dev/null || true
nmcli con add type ethernet ifname "${IFACE}" con-name "${IFACE}-scca" \
    ipv4.method manual ipv4.addresses "${PC_IP}/${PREFIX}" \
    ipv4.gateway "" ipv4.dns "" \
    ipv6.method ignore autoconnect yes
nmcli con up "${IFACE}-scca"

echo ""
echo "Teste: ping 10.0.0.1"
ip -4 addr show dev "${IFACE}"
