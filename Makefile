## ─────────────────────────────────────────────────────────────────────────────
## FarmaCompara Node Agent — Makefile
## Ejecuta `make help` para ver todos los comandos.
## ─────────────────────────────────────────────────────────────────────────────

.DEFAULT_GOAL := help

-include .env
AGENT_KEY ?= $(NODE_AGENT_API_KEY)
AGENT_URL ?= http://localhost:8765

BOLD  := \033[1m
RESET := \033[0m
CYAN  := \033[36m

.PHONY: help
help: ## Mostrar esta ayuda
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z_-]+:.*##/{printf "  $(CYAN)%-20s$(RESET) %s\n",$$1,$$2} /^##/{printf "\n$(BOLD)%s$(RESET)\n",substr($$0,3)}' $(MAKEFILE_LIST)

## ── Ciclo de vida ────────────────────────────────────────────────────────────

.PHONY: init
init: ## Primer paso: copiar .env.example → .env
	@test -f .env && echo ".env ya existe, no se sobreescribe" || \
	    (cp .env.example .env && echo ".env creado — edita MANAGER_URL, NODE_AGENT_API_KEY y HMAC_SECRET")

.PHONY: build
build: ## Construir la imagen del agente
	docker compose build --no-cache

.PHONY: up
up: ## Arrancar el agente (construye si no existe la imagen)
	docker compose up -d

.PHONY: down
down: ## Detener el agente
	docker compose down

.PHONY: restart
restart: ## Reiniciar el agente (útil tras editar .env)
	docker compose restart node-agent

.PHONY: logs
logs: ## Seguir logs del agente
	docker compose logs -f node-agent

.PHONY: ps
ps: ## Ver estado del contenedor
	docker compose ps

## ── Registro ─────────────────────────────────────────────────────────────────

.PHONY: register
register: ## Registrar este nodo con el manager (auto-detecta IPs)
	@bash register.sh

.PHONY: register-manual
register-manual: ## Registrar con IPs manuales: make register-manual PUBLIC_IP=x.x.x.x VPN_IP=10.x.x.x
	@test -n "$(PUBLIC_IP)" || (echo "Uso: make register-manual PUBLIC_IP=x.x.x.x VPN_IP=10.x.x.x" && exit 1)
	@test -n "$(VPN_IP)"    || (echo "Uso: make register-manual PUBLIC_IP=x.x.x.x VPN_IP=10.x.x.x" && exit 1)
	@bash register.sh $(PUBLIC_IP) $(VPN_IP)

## ── Estado y diagnóstico ─────────────────────────────────────────────────────

.PHONY: ping
ping: ## Verificar que el agente responde (requiere que esté corriendo)
	@curl -s -H "X-Agent-Key: $(AGENT_KEY)" $(AGENT_URL)/ping | python3 -m json.tool

.PHONY: health
health: ## Ver estado de salud del agente (ejecuta todos los checks)
	@curl -s -H "X-Agent-Key: $(AGENT_KEY)" $(AGENT_URL)/health | python3 -m json.tool

.PHONY: metrics
metrics: ## Ver métricas de sistema (CPU, RAM, conexiones)
	@curl -s -H "X-Agent-Key: $(AGENT_KEY)" $(AGENT_URL)/metrics | python3 -m json.tool

.PHONY: shell
shell: ## Abrir shell dentro del contenedor (debug)
	docker compose exec node-agent sh

.PHONY: public-ip
public-ip: ## Detectar la IP pública de este servidor
	@curl -s https://ifconfig.me && echo ""
