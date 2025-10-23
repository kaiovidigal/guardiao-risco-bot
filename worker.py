import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException 
import time

# =================================================================
# 🔑 CREDENCIAIS E CONFIGURAÇÕES
# =================================================================

# --- ⚠️ PREENCHA AQUI SUAS CREDENCIAIS REAIS DO KWBET ⚠️ ---
KW_USER = "SEU_EMAIL_OU_USUARIO_AQUI" 
KW_PASS = "SUA_SENHA_AQUI"

# --- 🍪 CONFIGURAÇÃO DE COOKIES (PREENCHA APENAS SE O LOGIN ABAIXO FALHAR) 🍪 ---
KW_COOKIE_NAME = "" 
KW_COOKIE_VALUE = "" 
# ----------------------------------------

# URLs da Kwbet
LOGIN_URL = "https://kwbet.com/pt"
CRAPS_URL = "https://kwbet.com/pt/games/live-craps"

# XPATHs FINAIS E EXATOS (Baseados nas suas imagens)
SELECTORS = {
    # XPATH ESPECÍFICO 1: Botão 'ENTRAR' (usa a classe 'botao-entrar')
    "login_open_button": "//a[contains(@class, 'botao-entrar')]", 
    
    # XPATH ESPECÍFICO 2: Campo de Usuário (usa o id='username')
    "username_field": "//input[@id='username']",                  
    
    # XPATH ESPECÍFICO 3: Campo de Senha (usa o id='password')
    "password_field": "//input[@id='password']",                  
    
    # XPATH Genérico para o botão de SUBMIT (dentro do modal)
    "login_submit_button": "//button[@type='submit' or contains(text(), 'Entrar')]" 
}

# =================================================================
# ⚙️ FUNÇÕES
# =================================================================

def initialize_driver():
    """Inicializa o undetected_chromedriver com correções de compatibilidade (versão 119)."""
    options = uc.ChromeOptions()
    
    # Configurações essenciais para rodar no VPS/Servidor
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    print("Configurando o Driver UC (Anti-Detecção e Resolução Desktop)...")
    
    try:
        # Correção crítica de compatibilidade
        driver = uc.Chrome(
            options=options,
            version_main=119 
        ) 
        print("Driver inicializado com sucesso.")
        return driver
    except Exception as e:
        print(f"❌ ERRO AO INICIALIZAR O DRIVER UC. Falha de compatibilidade: {e}")
        raise 

def login_via_cookie(driver, cookie_name, cookie_value):
    """Tenta injetar um cookie de sessão para evitar o login pelo formulário."""
    if not cookie_name or not cookie_value:
        return False

    driver.get(LOGIN_URL)
    
    try:
        print(f"Tentando injetar cookie de sessão: {cookie_name}...")
        driver.add_cookie({
            'name': cookie_name,
            'value': cookie_value,
            'domain': 'kwbet.com',
            'path': '/',
            'secure': True,
            'httpOnly': True
        })
        driver.get(LOGIN_URL)
        time.sleep(5)
        
        # Elemento de confirmação de login
        logged_in_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//button[contains(text(), 'Depósito') or contains(text(), 'Conta')]"))
        )
        
        print("✅ LOGIN VIA COOKIE BEM-SUCEDIDO! Sessão injetada com sucesso.")
        return True
        
    except (TimeoutException, NoSuchElementException, WebDriverException) as e:
        print("❌ FALHA NO LOGIN VIA COOKIE: O cookie pode estar expirado ou incorreto.")
        return False


def login_to_form(driver, username, password):
    """Realiza o login na Kwbet preenchendo o formulário com XPATHs específicos."""
    driver.get(LOGIN_URL)
    print("Tentando login via formulário com XPATHs exatos...")
    wait = WebDriverWait(driver, 25)

    try:
        # 1. CLICA NO BOTÃO 'ENTRAR'
        print("Tentando abrir o modal de Login (XPATH EXATO)...")
        login_open_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, SELECTORS["login_open_button"]))
        )
        login_open_button.click()
        print("✅ Botão 'ENTRAR' clicado. Aguardando modal...")
        time.sleep(3) 
        
        # 2. ENCONTRA E PREENCHE O CAMPO DE USUÁRIO (XPATH EXATO)
        print("Preenchendo Usuário (XPATH EXATO)...")
        username_field = wait.until(
            EC.presence_of_element_located((By.XPATH, SELECTORS["username_field"]))
        )
        username_field.send_keys(username)
        print("✅ Usuário preenchido.")

        # 3. ENCONTRA E PREENCHE O CAMPO DE SENHA (XPATH EXATO)
        print("Preenchendo Senha (XPATH EXATO)...")
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
        print("✅ Botão Entrar final clicado. Aguardando redirecionamento...")
        
        # 5. VERIFICA O SUCESSO DO LOGIN
        time.sleep(10)
        
        if driver.current_url != LOGIN_URL and "login" not in driver.current_url.lower():
            return True
        else:
            print("AVISO: Falha após o SUBMIT. Pode ser senha errada ou CAPTCHA/segurança.")
            return False

    except (TimeoutException, NoSuchElementException) as e:
        # Se falhar aqui, a página pode não ter carregado o modal ou o site tem proteção ativa.
        print("\n=======================================================")
        print("❌ ERRO CRÍTICO NO LOGIN DE FORMULÁRIO.")
        print("CAUSA: O site está bloqueando a automação. Use o método de COOKIES.")
        print(f"DETALHES DO ERRO: {e}") 
        print("=======================================================\n")
        return False
    except Exception as e:
        print(f"❌ ERRO CRÍTICO INESPERADO: {e}")
        return False

def navigate_to_craps(driver):
    """Navega diretamente para a página do Craps."""
    print(f"Navegando para o Craps: {CRAPS_URL}")
    driver.get(CRAPS_URL)
    WebDriverWait(driver, 20).until(
        EC.url_to_be(CRAPS_URL)
    )
    print("✅ Chegou à página do Craps. A página da Evolution deve estar carregada.")

# =================================================================
# 🚀 FUNÇÃO PRINCIPAL
# =================================================================

def run_bot():
    """Fluxo principal: Inicialização, Login (Cookie > Formulário) e Navegação."""
    driver = None
    login_success = False
    try:
        # 1. Inicializa o Driver
        driver = initialize_driver()
        
        # 2. Tenta Login por Cookie (PRIORIDADE MÁXIMA)
        if KW_COOKIE_NAME and KW_COOKIE_VALUE:
            login_success = login_via_cookie(driver, KW_COOKIE_NAME, KW_COOKIE_VALUE)
        
        # 3. Se o Cookie falhar (ou não estiver configurado), tenta o Formulário (XPATHS EXATOS)
        if not login_success:
            login_success = login_to_form(driver, KW_USER, KW_PASS)
        
        if login_success:
            # 4. Navega para o Craps
            navigate_to_craps(driver)
            
            # 5. INÍCIO DO LOOP DE LEITURA
            print("\n=======================================================")
            print("🚀 SUCESSO! O bot está na página do Craps.")
            print("=======================================================\n")
            
            while True:
                # LÓGICA DE LEITURA E APOSTA AQUI
                print("Bot em execução... (Loop de leitura/aposta)")
                time.sleep(15) 
            
        else:
            print("NÃO FOI POSSÍVEL CONTINUAR: O login falhou por todos os métodos.")

    except Exception as e:
        print(f"ERRO CRÍTICO NO FLUXO PRINCIPAL: {e}")
    finally:
        if driver:
            print("Fechando Driver.")
            driver.quit()

if __name__ == "__main__":
    run_bot()
