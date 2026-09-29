import os
from google import genai

client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
for m in client.models.list():
    if "live" in m.name.lower() or "flash" in m.name.lower():
        print(f"Name: {m.name}, Display Name: {m.display_name}")
