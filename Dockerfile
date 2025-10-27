# Usa uma imagem base que já é otimizada para Web Scraping/Python.
# Você pode tentar também 'python:3.10-slim' se quiser começar do zero, 
# mas essa imagem base já costuma ter dependências comuns de sistema.
FROM python:3.10

# Variáveis de Ambiente para evitar que o Python faça cache de logs (bom para containers)
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Instala as dependências de sistema necessárias para o Chrome/Chromium. 
# Estas são as bibliotecas que geralmente faltam no Render e causam o erro.
RUN apt-get update && apt-get install -y \
    chromium \
    libnss3 \
    libgconf-2-4 \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Define o diretório de trabalho no container
WORKDIR /app

# Copia os arquivos de dependência do Python e instala
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do seu código
COPY . /app/

# Comando que será executado quando o container iniciar (substitua 'seu_script.py' pelo nome real do seu arquivo)
CMD ["python", "seu_script.py"] 
