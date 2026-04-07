import requests

url = "https://api.fastforex.io/fx/pairs"
params = {"api_key": "d93532815e-2d9b94688e-tcxqsn"}

response = requests.get(url, params=params, timeout=15)

print("STATUS:", response.status_code)
print("RESPONSE:")
print(response.text)
