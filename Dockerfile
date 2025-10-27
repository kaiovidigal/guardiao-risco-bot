# ======================================================================================
# DOCKERFILE DEFINITIVO: SOLUÇÃO PARA O ERRO 'EXIT CODE 100' NO RENDER
# Utiliza a imagem Playwright, que já inclui Python, Chromium e todas as dependências Linux
# ======================================================================================

# 1. IMAGEM BASE COMPLETA (SEM NECESSIDADE DE NENHUM 'APT-GET')
FROM mcr.microsoft.com/playwright/python:latest

# 2. DEFINIÇÕES GERAIS E DIRETÓRIO DE TRABALHO
ENV PYTHONUNBUFFERED 1
WORKDIR /app

# 3. CÓPIA DO CÓDIGO E INSTALAÇÃO DE DEPENDÊNCIAS PYTHON
# Instala as dependências do seu projeto (Selenium, undetected-chromedriver, etc.)
COPY requirements.txt /app/

# O Playwright já inclui a maioria das libs. Instalamos apenas as específicas do seu projeto.
RUN pip install --no-cache-dir -r requirements.txt

# Cópia do restante do código da aplicação
COPY . /app/

# 4. PORTA E COMANDO DE INICIALIZAÇÃO
# A porta é um placeholder, o Render usará a variável $PORT
EXPOSE 10000

# COMANDO DE INICIALIZAÇÃO DO SEU BOT
# *** IMPORTANTE: AJUSTE "seu_bot_principal.py" para o nome do SEU arquivo principal ***
CMD ["python", "seu_bot_principal.py"]
