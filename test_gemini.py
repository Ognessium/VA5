import os
from dotenv import load_dotenv
from livekit.plugins import google

load_dotenv()

try:
    print("Initializing model...")
    model = google.realtime.RealtimeModel(
        model="gemini-3.8-live",
        voice="Puck",
    )
    print("Model initialized successfully!")
except Exception as e:
    print(f"ERROR: {e}")
