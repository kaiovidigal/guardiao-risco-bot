# 1. IMAGEM BASE ATUALIZADA E COMPLETA
# Usar uma imagem completa (sem o '-slim') aumenta a chance de sucesso na instalação de dependências Linux.
# Recomendamos Python 3.11 ou mais recente, que usa uma base Debian moderna (Bullseye/Bookworm).
FROM python:3.11

# 2. DEFINIÇÃO DE VARIÁVEIS DE AMBIENTE
# Variável para evitar o buffer de saída do Python (boa prática)
ENV PYTHONUNBUFFERED 1
# Variável de ambiente para o Chrome/Chromium (útil para alguns drivers)
ENV CHROME_BIN /usr/bin/chromium

# 3. INSTALAÇÃO DE DEPENDÊNCIAS DO SISTEMA (A CORREÇÃO DO ERRO 100 ESTÁ AQUI)
# Agrupamos todos os comandos em uma única instrução RUN para eficiência.
RUN apt-get update && \
    # Instala ferramentas de build e pacotes necessários para Chromium
    apt-get install -y --allow-unauthenticated --fix-missing \
        build-essential \
        chromium \
        libnss3 \
        libgconf-2-4 && \
    # Limpa o cache do apt-get (mantido por boa prática)
    rm -rf /var/lib/apt/lists/*

# 4. CONFIGURAÇÃO DO DIRETÓRIO DE TRABALHO
WORKDIR /app

# 5. CÓPIA DO CÓDIGO E INSTALAÇÃO DE DEPENDÊNCIAS PYTHON
# Instala as dependências Python (ex: selenium, undetected-chromedriver)
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Cópia do restante do código da aplicação
COPY . /app/

# 6. PORTA E COMANDO DE INICIALIZAÇÃO
# A porta é definida pelo Render, mas você pode usar 10000 como placeholder.
EXPOSE 10000

# 7. COMANDO DE INICIALIZAÇÃO DO SEU BOT
# Substitua 'seu_bot_principal.py' pelo nome do arquivo Python que contém a lógica do seu bot.
# Se você usa Flask/Django/Gunicorn, este comando será diferente.
CMD ["python", "seu_bot_principal.py"]
