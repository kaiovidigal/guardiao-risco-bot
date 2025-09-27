FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Render injeta $PORT; use sh -c para expandir a variável
CMD ["sh", "-c", "uvicorn webhook_app:app --host 0.0.0.0 --port ${PORT:-8000}"]
