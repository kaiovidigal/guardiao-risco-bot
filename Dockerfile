# Usa uma imagem oficial do Selenium com Python e Chrome já configurados
# A tag "4.12.0-20230919" é um exemplo de uma versão estável
FROM selenium/standalone-chrome:4.12.0-20230919

# O Render precisa que o Python seja o usuário principal para rodar scripts
USER root 

# Define o diretório de trabalho
WORKDIR /usr/src/app

# Passo 1: Instalar dependências Python
# Instala o Python (se já não estiver na imagem base) e o pip
RUN apt-get update && apt-get install -y python3 python3-pip

# Copia os arquivos de requisitos e instala as bibliotecas
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Copia o restante do código (seu worker.py, etc.)
COPY . .

# Comando de início do Worker
# NOTE: O driver (Chrome) já está no PATH
CMD [ "python3", "worker.py" ]
