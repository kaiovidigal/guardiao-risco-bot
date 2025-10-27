# ======================================================================================
# DOCKERFILE FINAL: PRONTO PARA DEPLOY NO RENDER
# Imagem Playwright (resolve o erro apt-get)
# Nome do arquivo de execução CORRIGIDO para worker.py
# ======================================================================================

# 1. IMAGEM BASE COMPLETA (contém Python, Chromium e todas as libs Linux)
FROM mcr.microsoft.com/playwright/python:latest

# 2. DEFINIÇÕES GERAIS E DIRETÓRIO DE TRABALHO
ENV PYTHONUNBUFFERED 1
WORKDIR /app

# 3. CÓPIA DO CÓDIGO E INSTALAÇÃO DE DEPENDÊNCIAS PYTHON
# Instala as dependências do seu projeto (Selenium, undetected-chromedriver, etc.)
COPY requirements.txt /app/

# Instala as libs Python do seu arquivo
RUN pip install --no-cache-dir -r requirements.txt

# Cópia do restante do código da aplicação
COPY . /app/

# 4. PORTA E COMANDO DE INICIALIZAÇÃO
EXPOSE 10000

# COMANDO DE INICIALIZAÇÃO CORRIGIDO
# O nome do arquivo foi corrigido para 'worker.py' (conforme seu repositório)
CMD ["python", "worker.py"]
