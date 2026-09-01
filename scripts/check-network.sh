#!/bin/bash
# Verifica link entre dashboard (10.0.0.5) e Raspberry (10.0.0.1).
set -euo pipefail

DASHBOARD_IP="${SCCA_DASHBOARD_IP:-10.0.0.5}"
RASPBERRY_IP="${SCCA_COMMAND_HOST:-10.0.0.1}"

echo "=== Interface com IP 10.0.0.x ==="
ip -4 addr show | grep -E "10\.0\.0\." || echo "(nenhuma interface em 10.0.0.0/24)"

echo ""
echo "=== Rota para Raspberry (${RASPBERRY_IP}) ==="
ip route get "${RASPBERRY_IP}" 2>/dev/null || echo "sem rota"

echo ""
echo "=== Ping Raspberry ==="
if ping -c 3 -W 2 "${RASPBERRY_IP}"; then
    echo "OK: Raspberry responde ao ping."
else
    echo "FALHA: ${RASPBERRY_IP} inalcancavel."
    echo "  - Na Raspberry: sudo bash raspberry_pi/scripts/configure-static-ip.sh eth0"
    echo "  - Confirme cabo ethernet e IP 10.0.0.1 na Pi"
fi

echo ""
echo "=== Portas UDP (dashboard escuta 5006, Pi escuta 5005) ==="
ss -ulnp 2>/dev/null | grep -E ":500[56]\b" || echo "(nenhum processo escutando 5005/5006 nesta maquina)"
