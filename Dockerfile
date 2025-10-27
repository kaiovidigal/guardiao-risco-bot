# 1. IMAGEM BASE COM CHROME E DRIVER PRONTOS (Selenium Standalone)
# Esta imagem vem com o Chromium, o Driver (ChromeDriver) e uma base Linux pronta.
# Você só precisa adicionar o Python e suas dependências.
# A tag '4.8.3-20230301' é um exemplo estável, mas você pode usar 'latest' ou outra de sua preferência.
FROM selenium/standalone-chrome:latest

# 2. INSTALAÇÃO DO PYTHON (É NECESSÁRIO INSTALAR O PYTHON EM CIMA DESSA IMAGEM)
# A imagem base 'selenium/standalone-chrome' é baseada em Debian, mas não tem o Python por padrão.
RUN apt-get update && \
    apt-get install -y python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

# 3. CONFIGURAÇÃO DO DIRETÓRIO DE TRABALHO
WORKDIR /app

# 4. CÓPIA DO CÓDIGO E INSTALAÇÃO DE DEPENDÊNCIAS PYTHON
# Instala as dependências Python (selenium, undetected-chromedriver, etc.)
COPY requirements.txt /app/
# Use python3 e pip3 para garantir que as versões corretas sejam usadas
RUN pip3 install --no-cache-dir -r requirements.txt

# Cópia do restante do código da aplicação
COPY . /app/

# 5. AJUSTE DO COMANDO DE INICIALIZAÇÃO
# A porta é definida pelo Render.
EXPOSE 8080 

# COMANDO DE INICIALIZAÇÃO DO SEU BOT
# Lembre-se: O Selenium precisa se conectar ao ChromeDriver/Servidor da Selenium. 
# Se seu bot executa o navegador localmente, você precisa adaptá-lo.
# Se seu bot usa undetected-chromedriver (que gerencia o driver internamente), ele deve funcionar.
CMD ["python3", "seu_bot_principal.py"]
