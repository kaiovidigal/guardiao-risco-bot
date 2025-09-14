# api_fanta.py
import requests

API_USER = "Kaio"
API_PASS = "Kaio"
API_URL = "http://189.1.172.114:8080/api-evolution/Fan-Tan/result.json"

def get_latest_result():
    """Busca o último resultado do Fan-Tan via API"""
    try:
        resp = requests.get(API_URL, auth=(API_USER, API_PASS), timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("Erro na API:", e)
        return None

