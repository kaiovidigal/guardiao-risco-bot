FROM python:3.11-slim

# Evita prompts
ENV DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=0 \
    PYTHONUNBUFFERED=1

# Dependências básicas do sistema (para Playwright/Chromium)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget ca-certificates fonts-liberation \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-