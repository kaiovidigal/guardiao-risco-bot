# worker.py
# Código Python para monitoramento de sinais do Crazy Time (Web Scraping)

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service # NOVO IMPORT ESSENCIAL
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import logging

# ====================================================================
# CONFIGURAÇÃO
# ====================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# URL do site (Ajuste se necessário)
URL = "https://gamblingcounting.com" 

# SELETOR CSS: ESTE É UM PLACEHOLDER. AJUSTE ESTE SELETOR
# para a área de histórico das rodadas no seu site.
RESULT_SELECTOR = ".history-container .result-row" 

# Caminho do Driver (ChromeDriver) e do Binário (Chromium) na imagem Playwright
CHROME_DRIVER_PATH = "/usr/local/bin/chromedriver" # Caminho comum do driver
CHROME_BIN_PATH = "/usr/bin/chromium"              # Caminho comum do binário do navegador

# ====================================================================
# FUNÇÕES DO BOT
# ====================================================================

def setup_browser():
    """
    Configura e retorna o driver do Chrome usando a classe Service para
    definir os caminhos do driver e do binário.
    """
    logging.info("Iniciando a configuração do navegador...")
    chrome_options = Options()
    
    # Argumentos essenciais para rodar em Docker/Render (headless)
    chrome_options.add_argument("--headless") 
    chrome_options.add_argument("--no-sandbox") 
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    
    # ** CORREÇÃO FINAL: Define o caminho do binário do Chromium **
    chrome_options.binary_location = CHROME_BIN_PATH
    
    # ** CORREÇÃO FINAL: Define o caminho do ChromeDriver **
    service = Service(executable_path=CHROME_DRIVER_PATH)

    try:
        # Inicializa o Chrome usando o Service e as Options
        driver = webdriver.Chrome(service=service, options=chrome_options) 
        
        logging.info("Navegador Chrome iniciado com sucesso.")
        return driver
    except Exception as e:
        logging.error(f"ERRO CRÍTICO AO INICIAR O DRIVER/CHROME: {e}")
        return None

def fetch_signals(driver):
    """Navega até o site e extrai os últimos sinais."""
    try:
        logging.info(f"Navegando para: {URL}")
        driver.get(URL)
        
        # Usa espera explícita para o elemento de histórico carregar (mais robusto)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, RESULT_SELECTOR))
        )
        
        results = driver.find_elements(By.CSS_SELECTOR, RESULT_SELECTOR)
        
        if not results:
            logging.warning("Não encontrou nenhum resultado. Verifique o seletor CSS.")
            return []

        signals = []
        # Exemplo de extração: extrai o texto ou atributo de valor de cada resultado
        for result in results:
            try:
                # Adapte a extração conforme necessário
                value = result.get_attribute("data-value") or result.text.strip()
                signals.append(value)
            except:
                continue
                
        logging.info(f"Total de sinais capturados: {len(signals)}. Últimos: {signals[:5]}")
        return signals

    except Exception as e:
        logging.error(f"Erro ao capturar sinais: {e}")
        return []

def filter_and_alert(signals):
    """
    Sua lógica personalizada de filtragem de sinais e envio de alerta vai aqui.
    """
    if not signals:
        return

    logging.info("Iniciando a lógica de filtragem de sinais...")
    
    # --- EXEMPLO DE LÓGICA DE FILTRAGEM ---
    # Pegue os 5 sinais mais recentes
    recent_signals = signals[:5] 
    contagem_do_1 = recent_signals.count('1') 

    if contagem_do_1 >= 4:
        # AÇÃO DE ALERTA: Aqui você integraria uma função para enviar mensagem ao Telegram.
        logging.warning(f"*** SINAL DETECTADO ***: '1' apareceu {contagem_do_1} vezes em 5 rodadas.")
        # EX: send_telegram_message("SINAL DE ENTRADA DETECTADO!")
    else:
        logging.info("Nenhum padrão de sinal detectado na última análise.")
    # --------------------------------------


# ====================================================================
# LÓGICA DE EXECUÇÃO PRINCIPAL DO BOT
# ====================================================================
if __name__ == "__main__":
    driver = setup_browser()
    
    if driver:
        # Loop principal: verifica, filtra e espera
        while True:
            signals = fetch_signals(driver)
            filter_and_alert(signals)
            
            logging.info("Aguardando 60 segundos para a próxima verificação...")
            time.sleep(60) # Intervalo entre as verificações
            
        driver.quit()
    else:
        logging.error("O bot não pode ser executado devido à falha de inicialização do navegador.")
