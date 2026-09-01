#!/bin/bash
# Rode NA RASPBERRY PI para diagnosticar rede com o dashboard (10.0.0.5).
set -euo pipefail

DASHBOARD_IP="${SCCA_DASHBOARD_IP:-10.0.0.5}"
PI_IP_EXPECTED="10.0.0.1"

echo "=== Interfaces IPv4 ==="
ip -4 addr show

echo ""
echo "=== Interfaces ethernet ==="
for iface in eth0 end0 enx*; do
    [ -d "/sys/class/net/$iface" ] || continue
    echo "--- $iface ---"
    ip link show "$iface" | head -1
    ip -4 addr show dev "$iface" 2>/dev/null || echo "(sem IPv4)"
    carrier=$(cat "/sys/class/net/$iface/carrier" 2>/dev/null || echo "?")
    echo "carrier (cabo): $carrier"
done

echo ""
echo "=== Rotas ==="
ip route show

echo ""
echo "=== Esperado neste projeto ==="
echo "  Raspberry (eth0/end0): ${PI_IP_EXPECTED}/24"
echo "  Dashboard (PC):        ${DASHBOARD_IP}/24"
echo "  Gateway:               NENHUM neste link direto"

echo ""
echo "=== Ping dashboard ==="
if ping -c 3 -W 2 "${DASHBOARD_IP}"; then
    echo "OK: dashboard alcancavel."
else
    echo "FALHA: ${DASHBOARD_IP} inalcancavel."
    echo "  Configure IP fixo: sudo bash $(dirname "$0")/configure-static-ip.sh eth0"
    echo "  Ou tente: sudo bash $(dirname "$0")/configure-static-ip.sh end0"
fi

echo ""
echo "=== ARP vizinhos ==="
ip neigh show | grep -E "10\.0\.0\." || echo "(nenhum vizinho 10.0.0.x na tabela ARP)"
