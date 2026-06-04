#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# register.sh — Registra este nodo con el Proxy Manager y actualiza el .env
#
# Uso:
#   ./register.sh                        # auto-detecta PUBLIC_IP
#   ./register.sh 181.53.22.10           # PUBLIC_IP manual (detrás de NAT/VPN)
#   ./register.sh 181.53.22.10 10.0.1.5  # PUBLIC_IP y VPN_IP manuales
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ENV_FILE=".env"

# ── Cargar .env ───────────────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env no encontrado. Ejecuta primero: cp .env.example .env"
    exit 1
fi
# shellcheck disable=SC2046
export $(grep -v '^#' "$ENV_FILE" | grep -v '^$' | xargs)

MANAGER_URL="${MANAGER_URL:-}"
NODE_AGENT_API_KEY="${NODE_AGENT_API_KEY:-}"

if [ -z "$MANAGER_URL" ] || [ "$MANAGER_URL" = "http://10.0.0.1:8000" ]; then
    echo "ERROR: Configura MANAGER_URL en el .env antes de registrar."
    exit 1
fi
if [ -z "$NODE_AGENT_API_KEY" ] || [ "$NODE_AGENT_API_KEY" = "CHANGE_ME_node_agent_api_key" ]; then
    echo "ERROR: Configura NODE_AGENT_API_KEY en el .env antes de registrar."
    exit 1
fi

# ── Determinar IPs ────────────────────────────────────────────────────────────
PUBLIC_IP="${1:-}"
VPN_IP="${2:-}"

if [ -z "$PUBLIC_IP" ]; then
    echo "Detectando IP pública (lo que ven las farmacias al scrapearte)..."
    PUBLIC_IP=$(curl -s --max-time 5 https://ifconfig.me \
             || curl -s --max-time 5 https://api.ipify.org \
             || curl -s --max-time 5 https://icanhazip.com | tr -d '\n')
    if [ -z "$PUBLIC_IP" ]; then
        echo "ERROR: No se pudo detectar la IP pública. Pásala como argumento:"
        echo "  ./register.sh 181.53.22.10"
        exit 1
    fi
    echo "  → IP pública detectada: $PUBLIC_IP"
fi

if [ -z "$VPN_IP" ]; then
    echo "Detectando IP de VPN (la que usará el manager para conectarse a este agente)..."
    # Intenta encontrar la IP de la interfaz VPN más común
    VPN_IP=$(ip route get "$( echo "$MANAGER_URL" | sed 's|http[s]*://||' | cut -d: -f1 )" 2>/dev/null \
             | grep -oP 'src \K\S+' | head -1 || true)
    if [ -z "$VPN_IP" ]; then
        # Fallback: primera IP privada no-loopback
        VPN_IP=$(hostname -I 2>/dev/null | tr ' ' '\n' \
                 | grep -E '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)' \
                 | head -1 || true)
    fi
    if [ -z "$VPN_IP" ]; then
        echo ""
        echo "No se pudo detectar la VPN IP automáticamente."
        read -rp "Introduce la IP de VPN de este nodo (ej. 10.0.1.5): " VPN_IP
    else
        echo "  → VPN IP detectada: $VPN_IP"
    fi
fi

# ── Registrar en el manager ───────────────────────────────────────────────────
echo ""
echo "Registrando nodo en $MANAGER_URL ..."
echo "  public_ip : $PUBLIC_IP"
echo "  vpn_ip    : $VPN_IP"
echo ""

RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$MANAGER_URL/nodes/register" \
    -H "X-API-Key: $NODE_AGENT_API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"public_ip\":\"$PUBLIC_IP\",\"vpn_ip\":\"$VPN_IP\"}")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" != "201" ]; then
    echo "ERROR: El manager respondió HTTP $HTTP_CODE"
    echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
    exit 1
fi

NODE_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['node_id'])")
NODE_JWT=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
STATUS=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")

echo "Registro exitoso:"
echo "  node_id : $NODE_ID"
echo "  status  : $STATUS"
echo ""

# ── Actualizar .env ───────────────────────────────────────────────────────────
# Eliminar entradas previas de NODE_ID y NODE_JWT si existen
grep -v '^NODE_ID=' "$ENV_FILE" | grep -v '^NODE_JWT=' > "${ENV_FILE}.tmp"
mv "${ENV_FILE}.tmp" "$ENV_FILE"

# Agregar los nuevos valores
echo "NODE_ID=$NODE_ID"  >> "$ENV_FILE"
echo "NODE_JWT=$NODE_JWT" >> "$ENV_FILE"

echo ".env actualizado con NODE_ID y NODE_JWT."
echo ""
echo "Reiniciando el agente para aplicar la nueva identidad..."
docker compose restart node-agent 2>/dev/null \
    || echo "(El agente no está corriendo aún — ejecuta: docker compose up -d)"

echo ""
echo "Listo. El agente empezará a enviar métricas al manager en ~15 segundos."
