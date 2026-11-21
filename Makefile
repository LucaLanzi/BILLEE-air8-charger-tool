# BILLEE Air8 charger tool
#
#   make            # same as `make help`
#   make setup      # install the Python backend + Node front end
#   make start      # run with the real charger (Bluetooth) -> http://localhost:3000
#   make sim        # run with the simulator, no charger needed
#   make test       # run the backend test suite
#
# Pass backend options with ARGS, e.g.  make start ARGS="--stop-hex '13 xx 00'"

ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help banner check setup start sim test backend frontend clean

help: ## Show this list of targets
	@$(MAKE) --no-print-directory banner
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-9s\033[0m %s\n", $$1, $$2}'

check: ## Check that uv and Node 18+ are installed
	@command -v uv >/dev/null 2>&1 || { \
	  echo "uv is not installed. Install it with one of:"; \
	  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"; \
	  echo "  brew install uv"; exit 1; }
	@command -v node >/dev/null 2>&1 || { \
	  echo "Node.js is not installed. Get Node 18+ from https://nodejs.org or: brew install node"; exit 1; }
	@node -e 'process.exit(+process.versions.node.split(".")[0] >= 18 ? 0 : 1)' || { \
	  echo "Node $$(node -v) is too old; need 18 or newer."; exit 1; }
	@echo "Found uv $$(uv --version | cut -d' ' -f2) and Node $$(node -v)."

setup: check ## Install everything (Python venv + npm packages)
	cd backend && uv sync
	cd frontend && npm install --no-fund --no-audit
	@$(MAKE) --no-print-directory banner
	@echo "Setup complete. Run 'make start' (or 'make sim' to try it without the charger)."

start: ## Start backend + UI for the real charger, then open http://localhost:3000
	@[ -d frontend/node_modules ] && [ -d backend/.venv ] || { echo "Run 'make setup' first."; exit 1; }
	@$(MAKE) --no-print-directory banner
	@echo "Open http://localhost:3000   (Ctrl+C to quit)"
	cd frontend && node start.js $(ARGS)

sim: ## Start with the simulated charger (no hardware needed)
	@$(MAKE) --no-print-directory start ARGS="--sim $(ARGS)"

test: ## Run the backend test suite
	cd backend && uv run pytest -q

backend: ## Run only the Python backend on port 8765
	cd backend && uv run air8-backend $(ARGS)

frontend: ## Run only the Node UI (expects the backend on port 8765)
	cd frontend && node server.js

clean: ## Remove installed dependencies (backend/.venv, frontend/node_modules)
	rm -rf backend/.venv frontend/node_modules

banner:
	@echo ""
	@echo "	   ██████╗ ██╗██╗     ██╗     ███████╗███████╗"
	@echo "	   ██╔══██╗██║██║     ██║     ██╔════╝██╔════╝"
	@echo "	   ██████╔╝██║██║     ██║     █████╗  █████╗"
	@echo "	   ██╔══██╗██║██║     ██║     ██╔══╝  ██╔══╝"
	@echo "	   ██████╔╝██║███████╗███████╗███████╗███████╗"
	@echo "	   ╚═════╝ ╚═╝╚══════╝╚══════╝╚══════╝╚══════╝"
	@echo ""
	@echo "                ISDT Air8 Charger Control"
	@echo "       Bluetooth LE backend (Python)  ·  Web UI (Node.js)"
	@echo ""
