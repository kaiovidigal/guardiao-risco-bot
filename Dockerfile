# Usa uma imagem base Python com os pacotes apt necessários
FROM python:3.11-slim

# Instala o Chrome e suas dependências (necessário para o Selenium rodar em ambiente sem interface)
RUN apt-get update && apt-get install -y \
    wget \
    unzip \
    libnss3 \
    libxss1 \
    libappindicator1 \
    libindicator7 \
    fonts-liberation \
    libasound2 \
    libnspr4 \
    libxcomposite1 \
    libgbm-dev 

# Baixa e instala o Google Chrome
RUN wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
RUN dpkg -i google-chrome-stable_current_amd64.deb; apt-get -fy install
RUN rm google-chrome-stable_current_amd64.deb

# Define o diretório de trabalho
WORKDIR /usr/src/app

# Copia os arquivos de requisitos e instala as bibliotecas Python
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do código
COPY . .

# Comando de início (o Render Worker irá rodar este arquivo)
CMD [ "python", "worker.py" ]
