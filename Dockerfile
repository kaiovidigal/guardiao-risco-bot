# Dockerfile

# Usa uma imagem base Python que é compatível com o Playwright
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

# Define o diretório de trabalho dentro do container
WORKDIR /app

# Copia o arquivo de dependências para o container
COPY requirements.txt .

# Instala as dependências Python
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do código para o container
COPY . .

# Comando para iniciar o bot
# O Render usará este comando para rodar seu worker.py
CMD ["python", "worker.py"]
