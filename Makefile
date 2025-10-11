.PHONY: setup build up down logs webhook

setup:
	pip install -r requirements.txt
	python -m playwright install chromium

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

webhook:
	@if [ -z "$$TG_BOT_TOKEN" ]; then echo "Defina TG_BOT_TOKEN no ambiente!"; exit 1; fi; \
	curl -s "https://api.telegram.org/bot$$TG_BOT_TOKEN/setWebhook?url=$$PUBLIC_URL/webhook"