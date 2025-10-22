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

# --- 🍪 CONFIGURAÇÃO DE COOKIES (PARA IGNORAR O FORMULÁRIO) 🍪 ---
# Use essas variáveis se o LOGIN AUTOMÁTICO FALHAR.
# Se estiverem vazias, o bot tentará o login via formulário.
KW_COOKIE_NAME = "NOME_DO_COOKIE_DE_SESSAO_AQUI" # Ex: "session_id" ou "auth_token"
KW_COOKIE_VALUE = "VALOR_DO_COOKIE_DE_SESSAO_AQUI" # O valor longo da sua sessão ativa
# ----------------------------------------

# URLs da Kwbet
LOGIN_URL = "https://kwbet.com/pt"
CRAPS_URL = "https://kwbet.com/pt/games/live-craps"

# XPATHs REVISADOS (Para a Kwbet - USO APENAS SE O COOKIE FALHAR)
SELECTORS = {
    # XPATH MAIS ABRANGENTE para o botão 'ENTRAR'
    "login_open_button": "//button[text()='ENTRAR'] | //a[text()='ENTRAR'] | //*[contains(text(), 'ENTRAR') and not(contains(text(), 'REGISTRE'))]", 
    # XPATH genérico final para o campo de Usuário/Email
    "username_field": "//input[@name='username' or @name='email' or @id='username' or @id='email' or @type='text' or @type='email']",                  
    "password_field": "//input[@type='password']",                  
    "login_submit_button": "//button[@type='submit' or contains(text(), 'Entrar')]" 
}

# =================================================================
# ⚙️ FUNÇÕES
# =================================================================

def initialize_driver():
    """Inicializa o undetected_chromedriver com correções de compatibilidade (versão 119)."""
    options = uc.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    print("Configurando o Driver UC (Anti-Detecção e Resolução Desktop)...")
    
    try:
        driver = uc.Chrome(
            options=options,
            version_main=119 # Correção crítica de compatibilidade
        ) 
        print("Driver inicializado com sucesso.")
        return driver
    except Exception as e:
        print(f"❌ ERRO AO INICIALIZAR O DRIVER UC. Falha de compatibilidade: {e}")
        raise 

def login_via_cookie(driver, cookie_name, cookie_value):
    """Tenta injetar um cookie de sessão para evitar o login pelo formulário."""
    if not cookie_name or not cookie_value:
        print("AVISO: Cookies de sessão não configurados. Tentando login via formulário...")
        return False

    driver.get(LOGIN_URL)
    
    try:
        print(f"Tentando injetar cookie de sessão: {cookie_name}...")
        # Adicionar o cookie
        driver.add_cookie({
            'name': cookie_name,
            'value': cookie_value,
            'domain': 'kwbet.com',
            'path': '/',
            'secure': True,
            'httpOnly': True
        })
        
        # Recarregar a página para aplicar o cookie e verificar o login
        driver.get(LOGIN_URL)
        time.sleep(5)
        
        # Tenta encontrar um elemento que só aparece após o login (ex: botão de depósito, nome do usuário).
        # Este é um XPATH genérico que você pode precisar ajustar.
        logged_in_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//button[contains(text(), 'Depósito') or contains(text(), 'Conta')]"))
        )
        
        print("✅ LOGIN VIA COOKIE BEM-SUCEDIDO! Sessão injetada com sucesso.")
        return True
        
    except (TimeoutException, NoSuchElementException) as e:
        print("❌ FALHA NO LOGIN VIA COOKIE: O site não reconheceu a sessão ou o cookie expirou.")
        print("Recomendado: Obtenha um novo cookie de sessão.")
        return False
    except WebDriverException as e:
        print(f"❌ ERRO CRÍTICO AO INJETAR COOKIE: {e}")
        return False


def login_to_form(driver, username, password):
    """Tenta realizar o login na Kwbet preenchendo o formulário."""
    driver.get(LOGIN_URL)
    print("Tentando login via formulário (Botão 'ENTRAR')...")
    wait = WebDriverWait(driver, 25)

    try:
        # 1. CLICA NO BOTÃO 'ENTRAR' NA PÁGINA INICIAL (Abre o modal)
        print("Tentando abrir o modal de Login...")
        login_open_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, SELECTORS["login_open_button"]))
        )
        login_open_button.click()
        print("✅ Botão 'ENTRAR' clicado. Aguardando modal...")
        time.sleep(3) 
        
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
        print("✅ Botão Entrar final clicado. Aguardando redirecionamento...")
        
        # 5. VERIFICA O SUCESSO DO LOGIN
        time.sleep(10)
        
        if driver.current_url != LOGIN_URL and "login" not in driver.current_url.lower():
            return True
        else:
            return False

    except (TimeoutException, NoSuchElementException) as e:
        print("\n=======================================================")
        print("❌ ERRO NO XPATH/TIMEOUT: O bot não conseguiu encontrar um elemento na tela.")
        print("CAUSA: XPATHs genéricos não são aceitos pela Kwbet. A ÚNICA SOLUÇÃO RESTANTE É USAR COOKIES.")
        print(f"DETALHES DO ERRO: {e}") 
        print("=======================================================\n")
        return False
    except Exception as e:
        print(f"❌ ERRO CRÍTICO INESPERADO DURANTE O LOGIN DE FORMULÁRIO: {e}")
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
        
        # 2. Tenta Login por Cookie
        if KW_COOKIE_NAME and KW_COOKIE_VALUE:
            login_success = login_via_cookie(driver, KW_COOKIE_NAME, KW_COOKIE_VALUE)
        
        # 3. Se o Cookie falhar (ou não estiver configurado), tenta o Formulário
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
