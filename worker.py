# worker.py

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import time
import logging

# Configuração básica de log
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# URL do site de onde você quer extrair os dados
URL = "https://gamblingcounting.com" # Ajuste se o caminho for diferente
# SELECTOR CSS: Você precisará inspecionar a página para encontrar o seletor exato
# Este é um exemplo de seletor que pode pegar a tabela de resultados.
RESULT_SELECTOR = ".history-container .result-row" 
# Se for a área do VAVADA, procure por classes específicas ou IDs.

def setup_browser():
    """Configura e retorna o driver do Chrome em modo headless."""
    logging.info("Iniciando a configuração do navegador...")
    chrome_options = Options()
    
    # Argumentos essenciais para rodar em Docker/Render (headless)
    chrome_options.add_argument("--headless") 
    chrome_options.add_argument("--no-sandbox") 
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    
    try:
        # A imagem Playwright/Selenium já tem o driver no PATH, não precisa de executable_path
        driver = webdriver.Chrome(options=chrome_options)
        logging.info("Navegador Chrome iniciado com sucesso.")
        return driver
    except Exception as e:
        logging.error(f"Erro ao iniciar o driver: {e}")
        # Se você usa 'undetected-chromedriver', use:
        # import undetected_chromedriver as uc
        # driver = uc.Chrome(options=chrome_options)
        return None

def fetch_signals(driver):
    """Navega até o site e extrai os últimos sinais."""
    try:
        logging.info(f"Navegando para: {URL}")
        driver.get(URL)
        
        # Espera um pouco para o JavaScript carregar a tabela de histórico
        time.sleep(5) 
        
        # Tenta encontrar a tabela de resultados (AJUSTE O SELETOR AQUI)
        results = driver.find_elements(By.CSS_SELECTOR, RESULT_SELECTOR)
        
        if not results:
            logging.warning("Não encontrou nenhum resultado com o seletor atual. O site pode ter mudado.")
            return []

        signals = []
        # Exemplo de extração: percorre os resultados e extrai o texto/atributo
        for result in results:
            # EXEMPLO: Se o valor da rodada estiver no atributo 'data-value'
            try:
                value = result.get_attribute("data-value") or result.text.strip()
                signals.append(value)
            except:
                continue
                
        # O histórico é geralmente lido de forma inversa (o mais recente primeiro)
        # Se você quer os mais recentes, você pode inverter ou pegar a ordem da tabela.
        logging.info(f"Sinais recentes capturados: {signals[:10]}") # Mostra os 10 primeiros
        return signals

    except Exception as e:
        logging.error(f"Erro ao capturar sinais: {e}")
        return []

def filter_and_alert(signals):
    """
    Sua lógica de filtragem de sinais e alerta vai aqui.
    """
    if not signals:
        return

    logging.info("Iniciando a lógica de filtragem de sinais...")
    
    # Exemplo de lógica simples: Se o número '1' aparecer 4 vezes nas últimas 5 rodadas.
    recent_signals = signals[:5]
    contagem_do_1 = recent_signals.count('1') # Adapte o valor para o que você extraiu

    if contagem_do_1 >= 4:
        # AÇÃO DE ALERTA: Aqui você enviaria a mensagem para o Telegram, por exemplo.
        logging.warning(f"PADRÃO DETECTADO! O '1' apareceu {contagem_do_1} vezes em 5 rodadas. ENVIANDO ALERTA!")
        # Exemplo de função de envio de Telegram: send_telegram_message("SINAL DE ENTRADA!")
    else:
        logging.info("Nenhum padrão de sinal detectado na última análise.")


# ====================================================================
# LÓGICA DE EXECUÇÃO PRINCIPAL
# ====================================================================
if __name__ == "__main__":
    driver = setup_browser()
    
    if driver:
        # Loop principal para monitorar o sinal em tempo real (a cada 60 segundos)
        while True:
            signals = fetch_signals(driver)
            filter_and_alert(signals)
            
            logging.info("Aguardando 60 segundos para a próxima verificação...")
            time.sleep(60)
            
        driver.quit() # Fechar o navegador ao sair do loop (se você adicionar uma condição de parada)
    else:
        logging.error("O bot não pode ser executado sem o driver do navegador.")
