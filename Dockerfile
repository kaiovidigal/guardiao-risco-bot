# Substitua a tag específica por uma versão mais estável e genérica.
# "4.15.0" é uma versão recente e estável do Selenium.
# Você pode sempre verificar a última versão no Docker Hub do Selenium.

# NOVO DOCKERFILE (APENAS A PRIMEIRA LINHA MUDOU)
FROM selenium/standalone-chrome:4.15.0 

# O Render precisa que o Python seja o usuário principal para rodar scripts
USER root 

# Define o diretório de trabalho
WORKDIR /usr/src/app

# Passo 1: Instalar dependências Python
RUN apt-get update && apt-get install -y python3 python3-pip

# Copia os arquivos de requisitos e instala as bibliotecas
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Copia o restante do código (seu worker.py, etc.)
COPY . .

# Comando de início do Worker
CMD [ "python3", "worker.py" ]
