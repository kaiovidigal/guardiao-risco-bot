import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from telegram import Bot

# --- VARIÁVEIS DE AMBIENTE (SUBSTITUA PELAS SUAS!) ---
# É melhor configurar isso como Environment Variables (Variáveis de Ambiente) no Render
CASINO_URL = os.environ.get("CASINO_URL", "https://site-do-seu-casino.com/craps")
LOGIN_USER = os.environ.get("LOGIN_USER", "SEU_LOGIN_AQUI")
LOGIN_PASS = os.environ.get("LOGIN_PASS", "SUA_SENHA_AQUI")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "SEU_TOKEN_DE_BOT")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-123456789") # ID do seu canal/grupo

# --- CONFIGURAÇÕES DE SCRAPING ---
# SELETORES DO SITE (MUITO IMPORTANTES - VOCÊ DEVE ENCONTRAR NO SITE REAL)
SELECTORS = {
    "campo_usuario": "id_do_campo_usuario",  # Ex: 'username'
    "campo_senha": "id_do_campo_senha",      # Ex: 'password'
    "botao_login": "xpath_do_botao_login",   # Ex: '//button[text()="Login"]'
    "historico_dados": "classe_ou_id_do_historico_dados", # Ex: '.dice-history-item:first-child'
    "botao_iniciar_jogo": "id_do_botao_iniciar_jogo", # Ex: para entrar na sala
}

# --- LÓGICA DO ROBÔ ---

def initialize_driver():
    """Configura e retorna o driver do Chrome."""
    print("Configurando o Chrome Driver...")
    chrome_options = Options()
    
    # Rodar em modo 'headless' (sem interface gráfica), essencial para servidores
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    
    # Adiciona um User-Agent para parecer menos como um bot
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36")
    
    # O Selenium Manager tenta encontrar o driver automaticamente, mas no Docker,
    # ele usará o Chrome que instalamos no Dockerfile
    return webdriver.Chrome(options=chrome_options)

def perform_login(driver):
    """Realiza o login no cassino."""
    print(f"Tentando acessar: {CASINO_URL}")
    driver.get(CASINO_URL)
    
    # Espera até que o campo de usuário esteja visível
    wait = WebDriverWait(driver, 20)
    
    try:
        # 1. Inserir Login
        user_field = wait.until(EC.presence_of_element_located((By.ID, SELECTORS["campo_usuario"])))
        user_field.send_keys(LOGIN_USER)
        
        # 2. Inserir Senha
        pass_field = driver.find_element(By.ID, SELECTORS["campo_senha"])
        pass_field.send_keys(LOGIN_PASS)
        
        # 3. Clicar em Login
        login_button = driver.find_element(By.XPATH, SELECTORS["botao_login"])
        login_button.click()
        
        print("Login realizado. Aguardando a página carregar...")
        
        # Opcional: Esperar por um elemento que só aparece após o login
        time.sleep(5)
        
    except Exception as e:
        print(f"Erro no Login: {e}")
        driver.quit()
        raise

def scrape_craps_result(driver):
    """Extrai o resultado mais recente do dado."""
    try:
        # Espera até que o elemento do resultado do dado esteja presente
        wait = WebDriverWait(driver, 10)
        result_element = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SELECTORS["historico_dados"]))
        )
        
        # Tenta extrair o texto/valor do dado. (Pode precisar de mais lógica dependendo do site)
        latest_result = result_element.text.strip()
        return latest_result
        
    except Exception as e:
        print(f"Erro ao raspar o resultado do Craps: {e}")
        return None

def main_worker_loop():
    """Loop principal do Worker."""
    bot = Bot(token=TELEGRAM_TOKEN)
    driver = initialize_driver()
    
    try:
        perform_login(driver)
        
        last_result = "" # Armazena o último dado raspado
        
        while True:
            # 1. Raspar o resultado mais recente
            current_result = scrape_craps_result(driver)
            
            if current_result and current_result != last_result:
                print(f"Novo Resultado: {current_result}")
                
                # 2. Lógica do SINAL
                signal = analyze_craps_strategy(current_result)
                
                # 3. Enviar para o Telegram
                if signal:
                    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=signal)
                
                last_result = current_result
            
            # Aguarda um período para não sobrecarregar o site (ex: 5 segundos)
            time.sleep(5) 
            
    except Exception as e:
        print(f"Erro Crítico no Worker: {e}. Reiniciando em 60 segundos...")
        time.sleep(60)
        # O Render pode tentar reiniciar o Worker automaticamente,
        # mas adicionamos um delay aqui.
        
    finally:
        driver.quit()
        print("Driver encerrado.")

def analyze_craps_strategy(result):
    """
    Função de Lógica do Craps.
    Você precisa desenvolver sua estratégia aqui.
    """
    try:
        # Converte o resultado raspado para um número (SE for um número)
        result_sum = int(result) 
    except:
        # Se não for um número (ex: 'Point On'), retorna sem sinal
        return None 
    
    # --- EXEMPLO DE ESTRATÉGIA SIMPLES (SUBSTITUA PELA SUA!) ---
    # Se o resultado for 2, 3 ou 12 (Craps) no Come Out Roll, é Win para Don't Pass Line
    if result_sum in [2, 3, 12]:
        return f"🚨 NOVO SINAL (Resultado: {result_sum}) 🚨 \n🎯 Entrar: Don't Pass Line"
    # Se o resultado for 7 ou 11 (Natural) no Come Out Roll, é Win para Pass Line
    elif result_sum in [7, 11]:
        return f"🟢 SINAL (Resultado: {result_sum}) 🟢 \n🎯 Entrar: Pass Line"
    else:
        # Se for 4, 5, 6, 8, 9 ou 10, o Ponto é estabelecido, 
        # e a análise deve ser mais complexa (baseada no histórico)
        return None # Nenhuma entrada neste ponto do exemplo

if __name__ == "__main__":
    # O Worker deve rodar o loop principal
    print("Iniciando Worker de Craps no Render...")
    main_worker_loop()

