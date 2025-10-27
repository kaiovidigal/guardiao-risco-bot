# 1. IMAGEM BASE ATUALIZADA
# Use uma imagem recente e suportada (Bullseye ou Bookworm).
# Ajuste a versão do Python se necessário.
FROM python:3.11-slim-bullseye

# 2. DEFINIÇÃO DE VARIÁVEIS DE AMBIENTE (SE NECESSÁRIO)
ENV PYTHONUNBUFFERED 1
ENV CHROME_DRIVER_VERSION 120.0.6099.109  # Ajuste para a versão que você usa no código (opcional)

# 3. INSTALAÇÃO DE DEPENDÊNCIAS DO SISTEMA (APT-GET)
# Instalamos o build-essential e os pacotes necessários para o Chromium (incluindo o libgconf-2-4 e libnss3)
# O comando está separado e o --no-install-recommends foi removido para evitar problemas de dependência.
# O pacote 'chromium' foi substituído por 'chromium-browser' ou mantido, dependendo da imagem base.
# Se 'chromium' falhar, tente 'chromium-browser' ou 'google-chrome-stable'.

# Atualiza a lista de pacotes
RUN apt-get update 

# Instala as dependências do Chromium/navegador com --fix-missing
# Removida a opção "--no-install-recommends" para resolver dependências
RUN apt-get install -y --fix-missing \
    build-essential \
    chromium \
    libnss3 \
    libgconf-2-4 

# Tenta a limpeza do cache (mantido por boa prática)
RUN rm -rf /var/lib/apt/lists/*

# 4. CONFIGURAÇÃO DO DIRETÓRIO DE TRABALHO
WORKDIR /app

# 5. CÓPIA DO CÓDIGO E INSTALAÇÃO DE DEPENDÊNCIAS PYTHON
# Cópia do arquivo de requisitos e instalação
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Cópia do restante do código
COPY . /app/

# 6. PORTA DO SERVIÇO
# Render injeta a porta, mas é bom documentar
EXPOSE 10000 

# 7. COMANDO DE INICIALIZAÇÃO (AJUSTE CONFORME SEU PROJETO)
# Exemplo para um app Flask ou Django.
CMD ["python", "app.py"] 
# OU, se for Gunicorn: CMD ["gunicorn", "seu_app.wsgi:application", "--bind", "0.0.0.0:$PORT"]
