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

# 🔧 XPATHs inteligentes (adaptáveis)
SELECTORS = {
    # campo de usuário/email/telefone
    "username_field": "//input[contains(@placeholder,'email') or contains(@placeholder,'usu') or contains(@placeholder,'fone') or @type='email' or @name='username']",
    # campo de senha
    "password_field": "//input[@type='password' or contains(@placeholder,'senha') or @name='password']",
    # botão entrar/login/acessar
    "login_button": "//button[contains(translate(., 'ENTRARLOGINACESSAR', 'entrarloginacessar'),'entrar') or contains(., 'Login') or contains(., 'Acessar') or @type='submit']",
    # resultado do dado
    "dice_result": "div.current-score",
}

results_history = []
MAX_HISTORY = 10
last_scraped_result = None

# ==============================================================================
# 2. FUNÇÕES DE TELEGRAM E SELENIUM
# ==============================================================================

def send_telegram_message(message):
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
    try:
        driver.get(login_url)
        print(f"Tentando acessar a página de login: {login_url}...")
        time.sleep(8)

        user_field = WebDriverWait(driver, 40).until(
            EC.presence_of_element_located((By.XPATH, selectors["username_field"]))
        )
        print("Preenchendo credenciais...")
        user_field.send_keys(user)

        pass_field = driver.find_element(By.XPATH, selectors["password_field"])
        pass_field.send_keys(password)

        driver.find_element(By.XPATH, selectors["login_button"]).click()
        time.sleep(5)

        driver.get(CRAPS_URL)
        print("Login realizado. Navegando para a página do Craps...")
        time.sleep(10)
        return True
    except Exception as e:
        print(f"ERRO DE LOGIN: {e}")
        send_telegram_message("🚨 ERRO DE LOGIN no Craps: Seletor XPATH incorreto ou página alterada. 🚨")
        return False

def scrape_data(driver, selector):
    try:
        result_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )
        result_text = result_element.text.strip()
        try:
            return int(result_text)
        except ValueError:
            return result_text
    except Exception:
        return None

# ==============================================================================
# 3. LÓGICA DO BOT
# ==============================================================================

def analyze_craps_strategy(history):
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