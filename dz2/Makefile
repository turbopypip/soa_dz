.PHONY: generate up down test demo
PYTHON ?= python3
generate:
	$(PYTHON) scripts/generate.py
up:
	docker compose up --build -d --wait
down:
	docker compose down
test:
	docker compose exec -T api pytest -q
demo:
	docker compose exec -T api python scripts/demo.py
