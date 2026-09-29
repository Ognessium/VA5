#!/bin/bash
# Move to the directory where this script is located
cd "$(dirname "$0")"

echo "🎙️ Setting up VoiceAgentV5 LiveKit Environment..."

# Create a clean virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating new virtual environment (venv)..."
    python -m venv venv
    
    # Activate the environment
    source venv/bin/activate
    
    echo "Installing required packages..."
    pip install livekit-agents livekit-plugins-openai livekit-plugins-silero livekit-plugins-google google-genai python-dotenv
else
    # Activate existing environment
    source venv/bin/activate
fi

echo "✅ Environment ready! Launching LiveKit Agent..."

# Run as a module using the explicitly requested venv python to prevent forkserver sys.executable bugs
./venv/bin/python -m src.livekit_agent dev
