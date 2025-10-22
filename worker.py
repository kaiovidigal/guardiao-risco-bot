import os
import time
import requests 
# Importa o undetected_chromedriver
import undetected_chromedriver as uc 
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from datetime import datetime

# ==============================================================================
# 1. CONFIGURAÇÕES E VARIÁVEIS DE AMBIENTE
# ==============================================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
# Lembre-se de atualizar LOGIN_USER e LOGIN_PASS no Render para as credenciais do kwbet.net!
LOGIN_USER = os.getenv("LOGIN_USER")
LOGIN_PASS = os.getenv("LOGIN_PASS")

# --- NOVAS URLS DO KW BET ---
LOGIN_URL = "https://kwbet.net/?ref=Autoroleta" 
CRAPS_URL = "https://kwbet.net/live-casino/evolution/evo-oss-xs-craps" 

# XPATHs de LOGIN (Genéricos por Posição) - Podem precisar de ajuste se o layout for diferente
# O Kwbet usa um pop-up de login, então esses XPATHs genéricos devem funcionar.
SELECTORS = {
    "username_field": "(//input)[1]", 
    "password_field": "(//input)[2]",
    # O botão de login pode ter o texto 'ENTRAR' ou 'LOGIN'. Manteremos 'ENTRAR' por enquanto.
    "login_button": "//button[contains(., 'ENTRAR') or contains(., 'LOGIN')]", 
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
    """Configura o driver usando undetected_chromedriver para evitar a detecção do bot."""
    try:
        print("Configurando o Driver UC (Anti-Detecção e Resolução Desktop)...")
        
        options = uc.ChromeOptions()
        # O UC lida com o modo headless e anti-detecção de forma mais eficaz
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # Força a resolução de desktop para evitar redirecionamento para o mobile
        options.add_argument("--window-size=1920,1080") 
        
        # Inicia o driver usando UC
        driver = uc.Chrome(options=options)
        return driver
    except Exception as e:
        print(f"ERRO CRÍTICO ao inicializar o Driver UC: {e}")
        send_telegram_message("🚨 ERRO CRÍTICO: Falha ao iniciar o Driver UC. Verifique a instalação da biblioteca. 🚨")
        return None

def login_to_site(driver, login_url, user, password, selectors):
    """Realiza o login com tolerância máxima de tempo e disfarce humano."""
    try:
        driver.get(login_url)
        print(f"Tentando acessar a página do Kwbet: {login_url}...")
        
        # Espera de 15s para a página carregar completamente
        time.sleep(15) 
        
        # --- NOVO PASSO: Clicar no botão de LOGIN/ENTRAR na página inicial ---
        # A kwbet tem um botão de login/entrar que abre o modal.
        try:
            # Tenta encontrar o botão que abre o modal de login na página inicial
            login_open_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'LOGIN') or contains(., 'ENTRAR')]"))
            )
            login_open_button.click()
            time.sleep(5) # Espera o modal de login abrir
        except Exception:
            print("Aviso: Botão de login inicial não encontrado ou não é necessário.")

        # Espera que o campo de usuário esteja INTERAGÍVEL (agora dentro do modal)
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

        # --- AÇÃO HUMANIZADA ---
        time.sleep(2) # Pequeno atraso antes de clicar em ENTRAR

        # Clicar no botão de login do MODAL
        driver.find_element(By.XPATH, selectors["login_button"]).click()
        time.sleep(5) 

        # --- NAVEGAÇÃO PARA O JOGO ---
        driver.get(CRAPS_URL)
        print("Login realizado. Navegando para a página do Craps...")
        
        # Espera 30s para o jogo carregar
        time.sleep(30) 
        
        # === TENTATIVA DE MUDAR PARA O IFRAME DO JOGO ===
        try:
            iframe = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "iframe"))
            )
            driver.switch_to.frame(iframe)
            print("Sucesso: Foco alterado para o Iframe do jogo.")
        except Exception:
            print("Aviso: Iframe do jogo não encontrado. Tentando raspar do host.")
            pass
            
        return True
        
    except Exception as e:
        print(f"ERRO DE LOGIN: {e}")
        send_telegram_message("🚨 ERRO CRÍTICO DE LOGIN: Falha de Autenticação (Kwbet). Verifique as credenciais do Kwbet! 🚨")
        return False

def scrape_data(driver, selectors_list):
    """Raspa o último resultado, tentando host e depois iframe, com 20s de espera total."""
    
    current_result = None
    
    # 1. Garante que o driver está no CONTEXTO PRINCIPAL antes de começar
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
        
    # === A) Tenta raspar no HOST (página principal) ===
    for i in range(0, len(selectors_list), 2):
        by_type = selectors_list[i]
        selector_value = selectors_list[i+1]
        try:
            result_element = WebDriverWait(driver, 10).until( 
                EC.presence_of_element_located((by_type, selector_value))
            )
            result_text = result_element.text.strip()
            if result_text.isdigit():
                return int(result_text)
            return result_text
        except Exception:
            continue
    
    # 2. Se falhou no Host, tenta mudar para o Iframe e raspar lá
    try:
        iframe = driver.find_element(By.TAG_NAME, "iframe")
        driver.switch_to.frame(iframe)
        
        # === B) Tenta raspar DENTRO do iframe ===
        for i in range(0, len(selectors_list), 2):
            by_type = selectors_list[i]
            selector_value = selectors_list[i+1]
            try:
                result_element = WebDriverWait(driver, 10).until( 
                    EC.presence_of_element_located((by_type, selector_value))
                )
                result_text = result_element.text.strip()
                if result_text.isdigit():
                    return int(result_text)
                return result_text
            except Exception:
                continue
                
    except Exception:
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

    if not login_to_site(driver, LOGIN_USER, LOGIN_PASS, SELECTORS):
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
            send_telegram_message(f"🚨 ERRO INESPERADO no Craps (Kwbet). Reiniciando Worker. Detalhe: {e} 🚨")
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
