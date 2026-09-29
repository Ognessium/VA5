import os
import asyncio
from dotenv import load_dotenv
from livekit.plugins import google
from livekit.agents import AgentSession

load_dotenv()

async def main():
    try:
        model = google.realtime.RealtimeModel(
            model="gemini-3.8-live",
            voice="Puck",
        )
        print("Model created.")
        session = AgentSession(llm=model, tools=[])
        print("Session created successfully.")
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(main())
