import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
# IMPORTAÇÃO CHAVE para capturar erros específicos de elemento/timeout
from selenium.common.exceptions import TimeoutException, NoSuchElementException 
import time

# =================================================================
# 🔑 CREDENCIAIS E CONFIGURAÇÕES
# =================================================================

# --- ⚠️ PREENCHA AQUI SUAS CREDENCIAIS REAIS DO KWBET ⚠️ ---
KW_USER = "SEU_EMAIL_OU_USUARIO_AQUI" 
KW_PASS = "SUA_SENHA_AQUI"
# ----------------------------------------

# URLs da Kwbet
LOGIN_URL = "https://kwbet.com/pt"
CRAPS_URL = "https://kwbet.com/pt/games/live-craps" # URL do Craps

# XPATHs genéricos da Kwbet (Tentativa para login)
SELECTORS = {
    # Tenta encontrar o botão "Entrar" na página inicial para abrir o modal
    "login_open_button": "//button[contains(text(), 'Entrar')]", 
    # Tenta encontrar o primeiro campo de entrada de texto ou email
    "username_field": "(//input[@type='email' or @type='text'])[1]",                  
    # Campo de senha
    "password_field": "//input[@type='password']",                  
    # Botão de envio (submit) dentro do modal
    "login_submit_button": "//button[@type='submit' or contains(text(), 'Entrar')]" 
}

# =================================================================
# ⚙️ FUNÇÕES
# =================================================================

def initialize_driver():
    """
    Inicializa o undetected_chromedriver com correções de compatibilidade.
    Força a versão 119 para corrigir o erro de 'session not created' no Render.
    """
    options = uc.ChromeOptions()
    
    # Configurações essenciais para rodar no VPS/Servidor
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    print("Configurando o Driver UC (Anti-Detecção e Resolução Desktop)...")
    
    try:
        # CORREÇÃO CRÍTICA: Força a versão 119 do Chrome (baseado no log de erro anterior)
        driver = uc.Chrome(
            options=options,
            version_main=119
        ) 
        print("Driver inicializado com sucesso.")
        return driver
    except Exception as e:
        print(f"❌ ERRO AO INICIALIZAR O DRIVER UC. A falha de compatibilidade persiste: {e}")
        raise 

def login_to_site(driver, username, password):
    """Tenta realizar o login na Kwbet com os XPATHs definidos, capturando erros específicos."""
    driver.get(LOGIN_URL)
    print(f"Tentando acessar a página de login: {LOGIN_URL}")
    
    # Aumentando o tempo de espera para 25 segundos para estabilidade no VPS/Render
    wait = WebDriverWait(driver, 25)

    try:
        # 1. CLICA NO BOTÃO 'ENTRAR' NA PÁGINA INICIAL (abre o modal)
        print("Tentando abrir o modal de Login (clicando no botão 'Entrar')...")
        login_open_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, SELECTORS["login_open_button"]))
        )
        login_open_button.click()
        print("✅ Modal de Login aberto (botão clicado).")
        time.sleep(2) 
        
        # 2. ENCONTRA E PREENCHE O CAMPO DE USUÁRIO
        print("Preenchendo Usuário...")
        username_field = wait.until(
            EC.presence_of_element_located((By.XPATH, SELECTORS["username_field"]))
        )
        username_field.send_keys(username)
        print("✅ Usuário preenchido.")

        # 3. ENCONTRA E PREENCHE O CAMPO DE SENHA
        print("Preenchendo Senha...")
        password_field = wait.until(
            EC.presence_of_element_located((By.XPATH, SELECTORS["password_field"]))
        )
        password_field.send_keys(password)
        print("✅ Senha preenchida.")

        # 4. CLICA NO BOTÃO FINAL DE SUBMISSÃO
        print("Clicando no botão 'Entrar' final...")
        login_submit_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, SELECTORS["login_submit_button"]))
        )
        login_submit_button.click()
        print("✅ Botão Entrar clicado. Aguardando redirecionamento...")
        
        # 5. VERIFICA O SUCESSO DO LOGIN
        time.sleep(10) # Pausa aumentada para estabilizar o redirecionamento
        
        # Verifica se a URL mudou e se não existe um elemento de erro de login
        if driver.current_url != LOGIN_URL and "login" not in driver.current_url.lower():
            print("✅ LOGIN BEM-SUCEDIDO! Acesso liberado.")
            return True
        else:
            print("❌ FALHA NO LOGIN: Permaneceu na página ou URL de login.")
            print("Isso pode ser devido a: XPATH errado, CAPTCHA ou verificação de segurança.")
            return False

    except (TimeoutException, NoSuchElementException) as e:
        # CAPTURA O ERRO ESPECÍFICO DO SELENIUM
        print("\n=======================================================")
        print("❌ ERRO NO XPATH/TIMEOUT: O bot não conseguiu encontrar um elemento na tela.")
        print("POSSÍVEL CAUSA: Os XPATHs genéricos estão incorretos para a Kwbet.")
        print(f"DETALHES DO ERRO: {e}") 
        print("=======================================================\n")
        return False
    except Exception as e:
        # Captura qualquer outro erro inesperado (rede, etc.)
        print(f"❌ ERRO CRÍTICO INESPERADO DURANTE O LOGIN: {e}")
        return False

def navigate_to_craps(driver):
    """Navega diretamente para a página do Craps."""
    print(f"Navegando para o Craps: {CRAPS_URL}")
    driver.get(CRAPS_URL)
    # Espera até que a URL do Craps seja totalmente carregada
    WebDriverWait(driver, 20).until(
        EC.url_to_be(CRAPS_URL)
    )
    print("✅ Chegou à página do Craps. A página da Evolution deve estar carregada.")

# =================================================================
# 🚀 FUNÇÃO PRINCIPAL
# =================================================================

def run_bot():
    """Fluxo principal do bot: Inicialização, Login e Navegação."""
    driver = None
    try:
        # 1. Inicializa o Driver
        driver = initialize_driver()
        
        # 2. Realiza o Login
        login_success = login_to_site(driver, KW_USER, KW_PASS)
        
        if login_success:
            # 3. Navega para o Craps
            navigate_to_craps(driver)
            
            # 4. INÍCIO DO LOOP DE LEITURA
            print("\n=======================================================")
            print("🚀 SUCESSO! O bot está na página do Craps.")
            print("O próximo passo é ler o iFrame e aplicar a estratégia.")
            print("=======================================================\n")
            
            # TODO: ADICIONAR LÓGICA DE LEITURA, SWITCH PARA O IFRAME E APOSTA
            while True:
                # Simula a leitura e espera (você vai substituir isso pela sua lógica)
                print("Bot em execução... (Loop de leitura/aposta)")
                time.sleep(15) 
            
        else:
            print("NÃO FOI POSSÍVEL CONTINUAR: O login falhou. Verifique o log acima para a causa.")

    except Exception as e:
        print(f"ERRO CRÍTICO NO FLUXO PRINCIPAL: {e}")
    finally:
        if driver:
            # Garante que o navegador feche
            print("Fechando Driver.")
            driver.quit()

if __name__ == "__main__":
    run_bot()

