import os
import time
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
LOGIN_USER = os.getenv("LOGIN_USER")
LOGIN_PASS = os.getenv("LOGIN_PASS")

LOGIN_URL = "https://m.luck.bet.br/signin?path=login" 
CRAPS_URL = "https://m.luck.bet.br/live-casino/game/1679419?provider=Evolution&from=%2Flive-casino%3Fname%3DCrap" 

# XPATHs de LOGIN (Genéricos por Posição, que funcionaram no log)
SELECTORS = {
    "username_field": "(//input)[1]", 
    "password_field": "(//input)[2]",
    "login_button": "//button[contains(., 'ENTRAR')]",
}

# SELETORES DO RESULTADO (Múltiplas Tentativas)
RESULT_SELECTORS = [
    By.CSS_SELECTOR, "div.current-score", 
    By.XPATH, "//div[contains(@class, 'score')]",
    By.XPATH, "//div[contains(@class, 'dice') and contains(@class, 'score')]",
    By.CSS_SELECTOR, "div[class*='number-roll']",
    By.XPATH, "//span[contains(@class, 'score') and text()]", 
]

results_history = []
MAX_HISTORY = 10 
last_scraped_result = None

# ==============================================================================
# 2. FUNÇÕES DE TELEGRAM E SELENIUM
# ==============================================================================

def send_telegram_message(message):
    """Envia mensagem usando requests (síncrono)."""
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": 'HTML'}
            response = requests.post(url, data=payload)
            response.raise_for_status()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Telegram: Mensagem enviada com sucesso.")
        except Exception as e:
            print(f"ERRO CRÍTICO ao enviar mensagem ao Telegram via Requests: {e}")

def initialize_driver():
    """Configura o driver para ambiente Docker/Headless."""
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
        send_telegram_message("🚨 ERRO CRÍTICO: Falha ao iniciar o Selenium. 🚨")
        return None

def login_to_site(driver, login_url, user, password, selectors):
    """Realiza o login com tolerância máxima de tempo e tenta mudar para Iframe."""
    try:
        driver.get(login_url)
        print(f"Tentando acessar a página de login: {login_url}...")
        
        # Espera de 15s para a página carregar completamente
        time.sleep(15) 

        # Espera que o campo de usuário esteja INTERAGÍVEL para evitar "not interactable"
        user_field = WebDriverWait(driver, 40).until(
            EC.element_to_be_clickable((By.XPATH, selectors["username_field"]))
        )
        print("Preenchendo credenciais...")
        user_field.send_keys(user)

        # Espera que o campo de senha esteja INTERAGÍVEL
        pass_field = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, selectors["password_field"]))
        )
        pass_field.send_keys(password)

        driver.find_element(By.XPATH, selectors["login_button"]).click()
        time.sleep(5) 

        # --- NAVEGAÇÃO PARA O JOGO ---
        driver.get(CRAPS_URL)
        print("Login realizado. Navegando para a página do Craps...")
        
        # Espera 30s para o jogo carregar (mesmo no Standard)
        time.sleep(30) 
        
        # === TENTATIVA DE MUDAR PARA O IFRAME DO JOGO ===
        try:
            # Tenta encontrar o iframe pelo nome da provedora (Evolution) ou pela tag
            iframe = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "iframe"))
            )
            driver.switch_to.frame(iframe)
            print("Sucesso: Foco alterado para o Iframe do jogo.")
        except Exception:
            # Se não conseguir, assume que não há iframe ou que o elemento do resultado está no host
            print("Aviso: Iframe do jogo não encontrado. Tentando raspar do host.")
            pass # Continua no host
            
        return True
        
    except Exception as e:
        # Se o login falhar (EVS incorreta), cairá neste erro
        print(f"ERRO DE LOGIN: {e}")
        send_telegram_message("🚨 ERRO CRÍTICO DE LOGIN: Credenciais, Timeout, ou Falha na Navegação para o Jogo. 🚨")
        return False

def scrape_data(driver, selectors_list):
    """Raspa o último resultado, tentando múltiplos seletores com espera de 20s."""
    for i in range(0, len(selectors_list), 2):
        by_type = selectors_list[i]
        selector_value = selectors_list[i+1]
        try:
            # Tempo de espera de 20s para o resultado aparecer (Timeout)
            result_element = WebDriverWait(driver, 20).until( 
                EC.presence_of_element_located((by_type, selector_value))
            )
            result_text = result_element.text.strip()
            # Tenta converter para inteiro, se for número
            if result_text.isdigit():
                return int(result_text)
            else:
                return result_text
        except Exception:
            continue
            
    # Tenta retornar ao contexto principal se não encontrar nada (caso o erro não tenha sido o iframe)
    try:
        driver.switch_to.default_content()
    except:
        pass
        
    return None

# ==============================================================================
# 3. LÓGICA DO BOT
# ==============================================================================

def analyze_craps_strategy(history):
    """Analisa o histórico e decide se deve enviar um sinal."""
    if len(history) < 5:
        return None
    last_five_sum = sum(history[-5:])
    
    if last_five_sum > 30:
        return f"🚨 NOVO SINAL (Soma: {last_five_sum}) 🚨\n🎯 Entrar: Don't Pass Line\n🎲 Próxima Rodada"
    return None

# ==============================================================================
# 4. LOOP PRINCIPAL
# ==============================================================================

def main_worker_loop():
    """Loop principal do Worker."""
    global last_scraped_result
    
    driver = initialize_driver()
    if driver is None:
        return

    if not login_to_site(driver, LOGIN_URL, LOGIN_USER, LOGIN_PASS, SELECTORS):
        driver.quit()
        return

    print("Worker de Craps pronto. Iniciando loop de raspagem...")

    while True:
        try:
            current_result = scrape_data(driver, RESULT_SELECTORS) 
            
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
            print(f"ERRO CRÍTICO NO LOOP: {e}")
            send_telegram_message(f"🚨 ERRO INESPERADO no Craps. Reiniciando Worker. Detalhe: {e} 🚨")
            driver.quit()
            return 

# ==============================================================================
# 5. EXECUÇÃO
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
