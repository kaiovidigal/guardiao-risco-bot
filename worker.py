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

# URLs e Seletor do Site (ATUALIZADO PARA LUCK.BET.BR)
# A URL de login será a URL base do site:
LOGIN_URL = "https://m.luck.bet.br" 

# URL direta para o jogo Craps (fornecido pelo usuário)
CRAPS_URL = "https://m.luck.bet.br/live-casino/game/1679419?provider=Evolution&from=%2Flive-casino%3Fname%3DCrap" 

# SELETORES (ATUALIZADO PARA LUCK.BET.BR - USANDO XPATH PARA ROBUSTEZ)
SELECTORS = {
    # XPATH: Encontra o input com o placeholder visível na tela de login (imagem 05:41)
    "username_field": "//input[@placeholder='Telefone, e-mail ou login *']", 
    "password_field": "//input[@placeholder='Senha *']", 
    
    # XPATH: Encontra o botão que contém o texto "ENTRAR"
    "login_button": "//button[contains(., 'ENTRAR')]",  
    
    # SELETOR DO RESULTADO DO DADO (CHUTE COMUM PARA EVOLUTION GAMING, PODE PRECISAR DE AJUSTE)
    # Tentei um seletor genérico para a área do resultado do dado (o número "2" na imagem 05:42)
    "dice_result": "div.current-score", # Se não funcionar, tente ajustar!
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
    """Realiza o login no site."""
    try:
        driver.get(login_url)
        print(f"Tentando acessar a página de login: {login_url}...")
        
        # Aumentamos o tempo de espera (8s) para que a página mobile carregue
        time.sleep(8) 
        
        # Espera EXPLICITAMENTE pelo campo de usuário usando XPATH
        user_field = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, selectors["username_field"]))
        )
        
        # Preenche os campos (usando XPATH para maior precisão)
        print("Preenchendo credenciais...")
        user_field.send_keys(user)
        driver.find_element(By.XPATH, selectors["password_field"]).send_keys(password)

        # Clica no botão de login (usando XPATH para o texto "ENTRAR")
        driver.find_element(By.XPATH, selectors["login_button"]).click()
        
        # Espera o login ser concluído
        time.sleep(5)
        
        # Navega diretamente para a página do jogo após o login
        driver.get(CRAPS_URL)
        print("Login realizado. Navegando para a página do Craps...")
        time.sleep(5) # Espera a página do jogo carregar
        
        return True
    except Exception as e:
        # Se o login falhar aqui, é quase sempre o seletor XPATH que está errado
        print(f"ERRO DE LOGIN (Seletor, Timeout ou Credenciais): {e}")
        send_telegram_message(f"🚨 ERRO DE LOGIN no Craps: Seletor/Credenciais incorretas na Luck.Bet. Verifique o XPATH. Reiniciando... 🚨")
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
        # print(f"Erro ao raspar dado: {e}")
        return None

# ==============================================================================
# 3. LÓGICA DO BOT (Não alterada)
# ==============================================================================

def analyze_craps_strategy(history):
    """
    Função de Lógica:
    Analisa o histórico e decide se deve enviar um sinal.
    (Exemplo Simples)
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
    if not login_to_site(driver, LOGIN_URL, LOGIN_USER, LOGIN_PASS, SELECTORS):
        driver.quit()
        return 

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
            
            # Espera 5 segundos para a próxima raspagem 
            time.sleep(5) 

        except Exception as e:
            # Qualquer erro inesperado
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
        
        # Espera 60 segundos antes de tentar reiniciar o worker
        print("Driver encerrado. Reiniciando em 60 segundos...")
        time.sleep(60)
