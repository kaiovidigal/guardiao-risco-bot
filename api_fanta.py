import requests
import time

API_USER = "Kaio"
API_PASS = "Kaio"
API_URL = "http://189.1.172.114:8080/api-evolution/Fan-Tan/result.json"

def get_latest_result():
    resp = requests.get(API_URL, auth=(API_USER, API_PASS), timeout=10)
    resp.raise_for_status()
    return resp.json()

if __name__ == "__main__":
    last = None
    while True:
        try:
            result = get_latest_result()
            if result != last:
                print("Novo resultado:", result)
                last = result
            time.sleep(2)
        except Exception as e:
            print("Erro:", e)
            time.sleep(5)
