# ======================================================================================
# DOCKERFILE FINAL: INSTALAÇÃO FORÇADA DO NAVEGADOR PLAYWRIGHT
# Resolve: Erro 'Executable does not exist'
# ======================================================================================

# 1. IMAGEM BASE COMPLETA PLAYWRIGHT
FROM mcr.microsoft.com/playwright/python:latest

# 2. DEFINIÇÕES GERAIS E DIRETÓRIO DE TRABALHO
ENV PYTHONUNBUFFERED 1
WORKDIR /app

# 3. CÓPIA DO CÓDIGO E INSTALAÇÃO DE DEPENDÊNCIAS PYTHON
COPY requirements.txt /app/
# Instala as libs Python (que agora deve ser apenas 'playwright')
RUN pip install --no-cache-dir -r requirements.txt

# *** CORREÇÃO CRÍTICA ***: Força a instalação dos binários do Chromium no contêiner.
RUN playwright install chromium

# Cópia do restante do código da aplicação
COPY . /app/

# 4. PORTA E COMANDO DE INICIALIZAÇÃO
EXPOSE 10000

# Nome do arquivo CORRIGIDO
CMD ["python", "worker.py"]
