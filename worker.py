import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

# =================================================================
# 🔑 CREDENCIAIS E CONFIGURAÇÕES
# =================================================================

# --- ALTERE AQUI COM SUAS CREDENCIAIS ---
KW_USER = "marechal.consultor@gmail.com" 
KW_PASS = "Serval@134234"
# ----------------------------------------

# URLs da Kwbet
LOGIN_URL = "https://kwbet.com/pt"
CRAPS_URL = "https://kwbet.com/pt/games/live-craps"

# XPATHs genéricos da Kwbet (Melhores palpites)
SELECTORS = {
    "login_open_button": "//button[contains(text(), 'Entrar')]", # Botão na home para abrir o modal de login
    "username_field": "(//input[@type='email' or @type='text'])[1]", # Campo de usuário/email
    "password_field": "//input[@type='password']",                  # Campo de senha
    "login_submit_button": "//button[@type='submit' or contains(text(), 'Entrar')]" # Botão de envio no modal
}

# =================================================================
# ⚙️ FUNÇÕES
# =================================================================

def initialize_driver():
    """Inicializa o undetected_chromedriver em modo headless (invisível no VPS)."""
    options = uc.ChromeOptions()
    
    # Configurações essenciais para rodar no VPS/Servidor
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    print("Inicializando Chrome Driver...")
    # Tenta resolver o problema de path do driver no VPS
    driver = uc.Chrome(options=options) 
    print("Driver inicializado com sucesso.")
    return driver

def login_to_site(driver, username, password):
    """Tenta realizar o login na Kwbet."""
    driver.get(LOGIN_URL)
    print(f"Navegando para: {LOGIN_URL}")
    
    wait = WebDriverWait(driver, 15)

    try:
        # 1. CLICA NO BOTÃO 'ENTRAR' NA PÁGINA INICIAL (abre o modal)
        print("Tentando abrir o modal de Login...")
        login_open_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, SELECTORS["login_open_button"]))
        )
        login_open_button.click()
        time.sleep(2) # Pequena pausa para o modal carregar
        
        # 2. ENCONTRA E PREENCHE O CAMPO DE USUÁRIO
        print("Preenchendo Usuário...")
        username_field = wait.until(
            EC.presence_of_element_located((By.XPATH, SELECTORS["username_field"]))
        )
        username_field.send_keys(username)

        # 3. ENCONTRA E PREENCHE O CAMPO DE SENHA
        print("Preenchendo Senha...")
        password_field = wait.until(
            EC.presence_of_element_located((By.XPATH, SELECTORS["password_field"]))
        )
        password_field.send_keys(password)

        # 4. CLICA NO BOTÃO FINAL DE SUBMISSÃO
        print("Clicando em Entrar...")
        login_submit_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, SELECTORS["login_submit_button"]))
        )
        login_submit_button.click()
        
        # 5. VERIFICA O SUCESSO DO LOGIN (espera por um elemento após o login, como um botão de perfil)
        time.sleep(5) # Pausa maior para o redirecionamento e carregamento
        print("Login tentado. Verificando...")
        
        # Se a URL não for mais a de login e não houver erro, considera-se sucesso
        if driver.current_url != LOGIN_URL:
            print("✅ LOGIN BEM-SUCEDIDO!")
            return True
        else:
            print("❌ FALHA NO LOGIN: Permaneceu na página de login. (Pode ser CAPTCHA ou XPATH errado)")
            return False

    except Exception as e:
        print(f"❌ ERRO GRAVE DURANTE O LOGIN: {e}")
        # Tira um print para debug (útil se estiver rodando localmente/com tela)
        # driver.save_screenshot("login_error.png") 
        return False

def navigate_to_craps(driver):
    """Navega diretamente para a página do Craps."""
    print(f"Navegando para o Craps: {CRAPS_URL}")
    driver.get(CRAPS_URL)
    WebDriverWait(driver, 20).until(
        EC.url_to_be(CRAPS_URL)
    )
    print("✅ Chegou à página do Craps.")

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
            # 3. Navega para o Craps e inicia a leitura/aposta
            navigate_to_craps(driver)
            
            # --- LOOP PRINCIPAL DO BOT AQUI ---
            print("\n=======================================================")
            print("🚀 PONTO DE INÍCIO DA LEITURA DE DADOS DO CRAPS (IFrame da Evolution)")
            print("Se essa mensagem aparecer, o login e a navegação deram certo!")
            print("=======================================================\n")
            
            # TODO: ADICIONAR LÓGICA DE LEITURA E APOSTA
            while True:
                # Aqui você irá ler o iFrame, aplicar a lógica do Craps e clicar nos botões.
                time.sleep(10) # Pausa para simular o loop de leitura
            
        else:
            print("NÃO FOI POSSÍVEL CONTINUAR: O login falhou.")

    except Exception as e:
        print(f"ERRO CRÍTICO NO FLUXO PRINCIPAL: {e}")
    finally:
        if driver:
            # Garante que o navegador feche no final ou em caso de erro
            print("Fechando Driver.")
            driver.quit()

if __name__ == "__main__":
    run_bot()

