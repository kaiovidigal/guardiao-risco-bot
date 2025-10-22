def login_to_site(driver, login_url, user, password, selectors):
    """Realiza o login com XPATHs genéricos, esperando a interação."""
    try:
        driver.get(login_url)
        print(f"Tentando acessar a página de login: {login_url}...")
        
        # Aumentamos a espera inicial de 8s para 12s para garantir que o overlay saia
        time.sleep(12) 

        # Espera EXPLICITAMENTE que o campo de usuário esteja INTERAGÍVEL
        user_field = WebDriverWait(driver, 40).until(
            EC.element_to_be_clickable((By.XPATH, selectors["username_field"]))
        )
        print("Preenchendo credenciais...")
        user_field.send_keys(user)

        # Espera EXPLICITAMENTE que o campo de senha esteja INTERAGÍVEL
        pass_field = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, selectors["password_field"]))
        )
        pass_field.send_keys(password)

        driver.find_element(By.XPATH, selectors["login_button"]).click()
        time.sleep(5) 

        driver.get(CRAPS_URL)
        print("Login realizado. Navegando para a página do Craps...")
        time.sleep(10)
        return True
    except Exception as e:
        print(f"ERRO DE LOGIN: {e}")
        # Mantenha o alerta de erro para monitoramento
        send_telegram_message("🚨 ERRO CRÍTICO DE LOGIN: Elemento não interativo ou Timeout. 🚨")
        return False
