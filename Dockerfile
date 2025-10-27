# Seção de Imagem Base (USE A SUA IMAGEM BASE ATUALIZADA AQUI)
FROM python:3.9-slim

# Garante que a lista de pacotes esteja atualizada
RUN apt-get update

# Instalação de pacotes (Use --fix-missing para tentar corrigir problemas de dependência)
# **ESTA É A LINHA CRÍTICA QUE ESTAVA FALHANDO**
RUN apt-get install -y --fix-missing chromium libnss3 libgconf-2-4 --no-install-recommends

# Limpeza do cache
RUN rm -rf /var/lib/apt/lists/*

# ... O RESTO DO SEU DOCKERFILE VAI AQUI ...
# (por exemplo: WORKDIR, COPY, RUN pip install -r requirements.txt, EXPOSE, CMD/ENTRYPOINT)
