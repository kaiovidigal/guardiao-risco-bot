import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from telegram import Bot
from datetime import datetime

# ==============================================================================
# 1. CONFIGURAÇÕES E VARIÁVEIS DE AMBIENTE
# ==============================================================================

# Variáveis de Ambiente (Configuradas no Render)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
LOGIN_USER = os.getenv("LOGIN_USER")
LOGIN_PASS = os.getenv("LOGIN_PASS")

# URLs e Seletor do Site (ADAPTE AO SEU SITE DE CRAPS!)
LOGIN_URL = "https://SEU_SITE_AQUI/login" # SUBSTITUA PELO SEU LINK
CRAPS_URL = "https://SEU_SITE_AQUI/game/craps" # SUBSTITUA PELO SEU LINK

# SELETORES (MUITO IMPORTANTES - VERIFIQUE NO SITE!)
# Use o Inspect Element para obter o ID, NAME ou XPATH correto.
SELECTORS = {
    "username_field": "input#username",  # Exemplo: input com ID 'username'
    "password_field": "input#password",  # Exemplo: input com ID 'password'
    "login_button": "button#login-btn",  # Exemplo: botão com ID 'login-btn'
    "dice_result": "div.craps-dice-result", # Exemplo: div com a classe 'craps-dice-result'
}

# Histórico e Lógica
results_history = []
MAX_HISTORY = 10
last_scraped_result = None

# ==============================================================================
# 2. FUNÇÕES DE AUTOMAÇÃO (SELENIUM)
# ==============================================================================

def send_telegram_message(message):
    """Envia uma mensagem ao Telegram."""
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            bot = Bot(token=TELEGRAM_TOKEN)
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML')
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Telegram: Mensagem enviada.")
        except Exception as e:
            print(f"ERRO ao enviar mensagem ao Telegram: {e}")

def initialize_driver():
    """Configura o driver do Chrome para rodar no Docker (headless)."""
    try:
        print("Configurando o Chrome Driver (Docker/Headless)...")
        chrome_options = Options()
        # Opções essenciais para rodar no ambiente Docker do Render:
        chrome_options.add_argument("--headless")       # Roda sem interface gráfica
        chrome_options.add_argument("--no-sandbox")     # Essencial para Linux no Docker
        chrome_options.add_argument("--disable-dev-shm-usage") # Otimiza uso de memória

        # Como estamos usando a imagem 'selenium/standalone-chrome', o driver já está no PATH
        driver = webdriver.Chrome(options=chrome_options)
        return driver
    except Exception as e:
        print(f"ERRO CRÍTICO ao inicializar o Selenium Driver: {e}")
        send_telegram_message(f"🚨 ERRO CRÍTICO no Worker Craps: Falha ao iniciar o Selenium. Verifique o Dockerfile ou a versão da imagem. 🚨")
        return None

def login_to_site(driver, login_url, user, password, selectors):
    """Realiza o login no site."""
    try:
        driver.get(login_url)
        print("Tentando acessar a página de login...")
        
        # AUMENTO DE TEMPO DE ESPERA PARA DAR TEMPO À PÁGINA DE CARREGAR
        time.sleep(8) 
        
        # Espera EXPLICITAMENTE pelo campo de usuário
        user_field = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selectors["username_field"]))
        )
        
        # Encontra e preenche os campos
        print("Preenchendo credenciais...")
        user_field.send_keys(user)
        driver.find_element(By.CSS_SELECTOR, selectors["password_field"]).send_keys(password)

        # Clica no botão de login
        driver.find_element(By.CSS_SELECTOR, selectors["login_button"]).click()
        
        # Espera o login ser concluído (Pode demorar, aumente o tempo se necessário)
        time.sleep(5)
        
        # Verifica se o login foi bem-sucedido navegando para a página do jogo
        driver.get(CRAPS_URL)
        print("Login realizado. Navegando para a página do Craps...")
        time.sleep(5) # Espera a página do jogo carregar
        
        return True
    except Exception as e:
        # Erro comum aqui é NoSuchElementException ou TimeoutException
        print(f"ERRO DE LOGIN (Seletor ou Timeout): {e}")
        send_telegram_message(f"🚨 ERRO DE LOGIN no Craps: Seletor/URL/Credenciais incorretas. Verifique seu worker.py e variáveis. Reiniciando... 🚨")
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
        # É comum dar erro se o dado estiver rolando ou o elemento sumir brevemente.
        # Não é um erro fatal para o loop, apenas um 'pass' para tentar de novo.
        # print(f"Erro ao raspar dado: {e}")
        return None

# ==============================================================================
# 3. LÓGICA DO BOT
# ==============================================================================

def analyze_craps_strategy(history):
    """
    Função de Lógica:
    Analisa o histórico e decide se deve enviar um sinal.
    (Exemplo Simples)
    """
    if len(history) < 5:
        return None # Precisa de pelo menos 5 resultados

    # Exemplo: Se os últimos 5 resultados somaram mais de 30, aposte no Don't Pass
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
        return # Encerrar se o driver falhar

    # Tenta fazer login
    if not login_to_site(driver, LOGIN_URL, LOGIN_USER, LOGIN_PASS, SELECTORS):
        driver.quit()
        return # Encerrar se o login falhar

    print("Worker de Craps pronto. Iniciando loop de raspagem...")
    
    # Loop de monitoramento principal (roda enquanto o container estiver ativo)
    while True:
        try:
            current_result = scrape_data(driver, SELECTORS["dice_result"])
            
            if current_result is not None and current_result != last_scraped_result:
                # É um novo resultado e é diferente do último
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Novo Resultado: {current_result}")
                
                # Atualiza histórico e último resultado
                if isinstance(current_result, int):
                    results_history.append(current_result)
                    if len(results_history) > MAX_HISTORY:
                        results_history.pop(0)
                        
                    # Verifica a lógica
                    signal = analyze_craps_strategy(results_history)
                    if signal:
                        send_telegram_message(signal)
                        
                last_scraped_result = current_result
            
            # Espera 5 segundos para a próxima raspagem (ou o tempo que o jogo exige)
            time.sleep(5) 

        except Exception as e:
            # Qualquer erro inesperado (ex: o elemento sumiu ou a sessão expirou)
            print(f"ERRO CRÍTICO NO LOOP: {e}. Reiniciando sessão...")
            send_telegram_message(f"🚨 ERRO INESPERADO no Craps. Reiniciando Worker. Detalhe: {e} 🚨")
            driver.quit()
            return # Sai da função, forçando o loop de reinicialização de 60s do Render

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
        
        # Espera 60 segundos antes de tentar reiniciar o worker
        print("Driver encerrado. Reiniciando em 60 segundos...")
        time.sleep(60)
