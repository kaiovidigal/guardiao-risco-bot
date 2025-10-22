# Usa uma imagem base Python que já tem dependências Linux essenciais
FROM python:3.11-slim

# Variáveis de ambiente para o Chrome e ChromeDriver
ENV CHROME_DRIVER_VERSION=114.0.5735.90
ENV CHROME_VERSION=114.0.5735.90

# Passo 1: Instalar dependências necessárias para o Chrome
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    libnss3 \
    libxss1 \
    libappindicator3 \
    libindicator7 \
    fonts-liberation \
    libasound2 \
    libnspr4 \
    libxcomposite1 \
    libgbm-dev \
    --no-install-recommends

# Passo 2: Baixar e instalar o Google Chrome
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list \
    && apt-get update && apt-get install -y google-chrome-stable

# O Render geralmente já tem o Chrome/Driver no PATH
# Mas instalaremos o driver mais atualizado via 'pip'

# Define o diretório de trabalho
WORKDIR /usr/src/app

# Passo 3: Copia os arquivos de requisitos e instala as bibliotecas Python
COPY requirements.txt ./
# O Selenium 4 não precisa mais do ChromeDriver separado se você usar Service.
# Certifique-se de que no seu requirements.txt você tenha: selenium==4.12.0 ou superior
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do código
COPY . .

# Comando de início do Worker
CMD [ "python", "worker.py" ]
