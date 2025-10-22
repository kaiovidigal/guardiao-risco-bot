import os
import time
# IMPORTANTE: Adicione requests ao seu requirements.txt se não estiver lá
import requests 

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from datetime import datetime

# ==============================================================================
# 1. CONFIGURAÇÕES E VARIÁVEIS DE AMBIENTE
# ==============================================================================

# Variáveis de Ambiente (Configuradas no Render)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
LOGIN_USER = os.getenv("LOGIN_USER")
LOGIN_PASS = os.getenv("LOGIN_PASS")

# URLs e Seletor do Site (LUCK.BET.BR)
# URL de login explícita para evitar redirecionamentos
LOGIN_URL = "https://m.luck.bet.br/signin?path=login" 
CRAPS_URL = "https://m.luck.bet.br/live-casino/game/1679419?provider=Evolution&from=%2Flive-casino%3Fname%3DCrap" 

# SELETORES (ÚLTIMA TENTATIVA: XPATH por Posição - Mais confiável em mobile/headless)
SELECTORS = {
    # Pega o primeiro campo de input na página, que deve ser o de usuário/email.
    "username_field": "(//input)[1]", 
    # Pega o segundo campo de input na página, que deve ser o de senha.
    "password_field": "(//input)[2]",
    
    # XPATH: Encontra o botão que contém o texto "ENTRAR"
    "login_button": "//button[contains(., 'ENTRAR')]",  
    
    # SELETOR DO RESULTADO DO DADO (O mais provável para Evolution Gaming)
    "dice_result": "div.current-score", 
}

# Histórico e Lógica
results_history = []
MAX_HISTORY = 10
last_scraped_result = None

# ==============================================================================
# 2. FUNÇÕES DE AUTOMAÇÃO (SELENIUM) E TELEGRAM
# ==============================================================================

def send_telegram_message(message):
    """Envia uma mensagem ao Telegram usando requests (síncrono e robusto)."""
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": 'HTML'
            }
            # Usa requests para garantir a sincronia e evitar o erro 'await'
            response = requests.post(url, data=payload)
            response.raise_for_status() # Lança exceção para códigos de status ruins
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Telegram: Mensagem enviada com sucesso.")
        except Exception as e:
            print(f"ERRO CRÍTICO ao enviar mensagem ao Telegram via Requests: {e}") 

def initialize_driver():
    """Configura o driver do Chrome para rodar no Docker (headless)."""
    try:
        print("Configurando o Chrome Driver (Docker/Headless)...")
        chrome_options = Options()
        chrome_options.add_argument("--headless")       
        chrome_options.add_argument("--no-sandbox")     
        chrome_options.add_argument("--disable-dev-shm-usage") 

        driver = webdriver.Chrome(options=chrome_options)
        return driver
    except Exception as e:
        print(f"ERRO CRÍTICO ao inicializar o Selenium Driver: {e}")
        send_telegram_message(f"🚨 ERRO CRÍTICO no Worker Craps: Falha ao iniciar o Selenium. 🚨")
        return None

def login_to_site(driver, login_url, user, password, selectors):
    """Realiza o login no site com XPATHs por posição e waits."""
    try:
        driver.get(login_url) # Navega para a URL explícita de login
        print(f"Tentando acessar a página de login: {login_url}...")
        
        # Aumentamos o tempo de espera para que a página mobile carregue
        time.sleep(8) 
        
        # Espera EXPLICITAMENTE pelo campo de usuário usando XPATH por posição
        user_field = WebDriverWait(driver, 40).until(
            EC.presence_of_element_located((By.XPATH, selectors["username_field"]))
        )
        
        # Preenche os campos
        print("Preenchendo credenciais...")
        user_field.send_keys(user)
        driver.find_element(By.XPATH, selectors["password_field"]).send_keys(password)

        # Clica no botão de login
        driver.find_element(By.XPATH, selectors["login_button"]).click()
        
        # Espera o login ser concluído
        time.sleep(5)
        
        # Navega diretamente para a página do jogo após o login
        driver.get(CRAPS_URL)
        print("Login realizado. Navegando para a página do Craps...")
        time.sleep(10) # Espera a página do jogo carregar
        
        return True
    except Exception as e:
        # Erro comum aqui é NoSuchElementException ou TimeoutException
        print(f"ERRO DE LOGIN (Seletor, Timeout ou Credenciais): {e}")
        send_telegram_message(f"🚨 ERRO DE LOGIN no Craps: Seletor XPATH incorreto. Reiniciando... 🚨")
        return False

def scrape_data(driver, selector):
    """Raspa o último resultado do dado."""
    try:
        # Espera pelo elemento do resultado do dado (tempo de 10s)
        result_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )
        
        result_text = result_element.text.strip()
        
        # Tenta converter para inteiro, se for um número
        try:
            return int(result_text)
        except ValueError:
            return result_text
            
    except Exception as e:
        return None

# ==============================================================================
# 3. LÓGICA DO BOT
# ==============================================================================

def analyze_craps_strategy(history):
    """
    Função de Lógica:
    Analisa o histórico e decide se deve enviar um sinal.
    """
    if len(history) < 5:
        return None 

    last_five_sum = sum(history[-5:])
    
    if last_five_sum > 30:
        return f"🚨 NOVO SINAL (Soma: {last_five_sum}) 🚨\n🎯 Entrar: Don't Pass Line\n🎲 Próxima Rodada"

    return None

# ==============================================================================
# 4. LOOP PRINCIPAL (WORKER 24/7)
# ==============================================================================

def main_worker_loop():
    """Loop principal do Worker."""
    global last_scraped_result
    
    driver = initialize_driver()
    if driver is None:
        return 

    # Tenta fazer login
    # As credenciais são lidas das variáveis de ambiente do Render
    if not login_to_site(driver, LOGIN_URL, LOGIN_USER, LOGIN_PASS, SELECTORS):
        driver.quit()
        return 

    print("Worker de Craps pronto. Iniciando loop de raspagem...")
    
    while True:
        try:
            current_result = scrape_data(driver, SELECTORS["dice_result"])
            
            if current_result is not None and current_result != last_scraped_result:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Novo Resultado: {current_result}")
                
                if isinstance(current_result, int):
                    results_history.append(current_result)
                    if len(results_history) > MAX_HISTORY:
                        results_history.pop(0)
                        
                    signal = analyze_craps_strategy(results_history)
                    if signal:
                        send_telegram_message(signal)
                        
                last_scraped_result = current_result
            
            time.sleep(5) 

        except Exception as e:
            print(f"ERRO CRÍTICO NO LOOP: {e}. Reiniciando sessão...")
            send_telegram_message(f"🚨 ERRO INESPERADO no Craps. Reiniciando Worker. Detalhe: {e} 🚨")
            driver.quit()
            return 

# ==============================================================================
# 5. INÍCIO DO PROGRAMA
# ==============================================================================

if __name__ == "__main__":
    print("Iniciando Worker de Craps no Render...")
    while True:
        try:
            main_worker_loop()
        except Exception as e:
            print(f"ERRO CRÍTICO PRINCIPAL: {e}")
        
        print("Driver encerrado. Reiniciando em 60 segundos...")
        time.sleep(60)
