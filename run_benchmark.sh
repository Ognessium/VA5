#!/bin/bash
# ============================================================================
# run_benchmark.sh — One-command FDB-v3 reproduction script for VoiceAgentV5
#
# This script:
#   1. Sets up the Python environment
#   2. Starts our dual-architecture agent
#   3. Runs the FDB-v3 inference pipeline against all 100 scenarios
#   4. Runs all evaluation steps (tool accuracy, pass rate, latency)
#
# Prerequisites:
#   - .env.local in Full-Duplex-Bench/v3/ with LIVEKIT + GOOGLE credentials
#   - fdb_v3_data_released/ in Full-Duplex-Bench/v3/ (downloaded from Drive)
#   - ffmpeg installed
#
# Usage:
#   ./run_benchmark.sh
#   ./run_benchmark.sh --force    # Overwrite existing results
# ============================================================================

set -e

PROVIDER="voiceagentv5"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
V3_DIR="$SCRIPT_DIR/Full-Duplex-Bench/v3"
FORCE_FLAG="${1:-}"

echo "═══════════════════════════════════════════════════════════════"
echo "  VoiceAgentV5 — FDB-v3 Benchmark Runner"
echo "═══════════════════════════════════════════════════════════════"

# ── 1. Environment Setup ────────────────────────────────────────────────────
echo ""
echo "📦 Step 1: Setting up environment..."

if [ ! -d "venv" ]; then
    echo "  Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    # Use hard drive for pip temp files (bypasses RAM disk quota limits on /tmp)
    mkdir -p tmp_pip
    export TMPDIR=$PWD/tmp_pip

    # Install heavy dependencies sequentially to prevent OOM on 16GB machines
    echo "  📦 Installing PyTorch..."
    pip install -q --no-cache-dir torch torchvision torchaudio
    
    echo "  📦 Installing general requirements..."
    pip install -q --no-cache-dir livekit-agents livekit-plugins-google livekit-plugins-silero \
                   google-genai python-dotenv pydub ffmpeg-python numpy openai
    pip install -q --no-cache-dir "livekit[crypto]~=1.0"
    
    echo "  📦 Installing Cython (required for NeMo)..."
    pip install -q --no-cache-dir Cython
    
    echo "  📦 Installing nemo_toolkit (ASR evaluation framework)..."
    pip install -q --no-cache-dir "nemo_toolkit[asr]"
    
    # Fix protobuf conflict caused by NeMo's strict requirements vs ONNX
    pip install -q --upgrade protobuf onnx
    
    # Clean up
    rm -rf tmp_pip
    echo "  ✅ Environment created and packages installed"
else
    source venv/bin/activate
    echo "  ✅ Using existing virtual environment"
fi

# Check for benchmark data
if [ ! -d "$V3_DIR/fdb_v3_data_released" ]; then
    echo ""
    echo "  ❌ Benchmark data not found at: $V3_DIR/fdb_v3_data_released/"
    echo "  Download it from: https://drive.google.com/file/d/1SO_4MTazWQ_jvCx0dtmpQ-t40bdd07yz/view"
    echo "  Extract and place the fdb_v3_data_released/ folder inside Full-Duplex-Bench/v3/"
    exit 1
fi

# ── 2. Pre-generate prompts ────────────────────────────────────────────────
echo ""
echo "🧠 Step 2: Pre-generating LLM prompts (if not cached)..."
if [ ! -f "cached_prompts.json" ]; then
    python -c "
import sys; sys.path.insert(0, '.')
from src.initializer import AgentInitializer
import json
init = AgentInitializer('tool_manifest.json', '.')
r, t = init.build_prompts()
with open('cached_prompts.json', 'w') as f:
    json.dump({'r_prompt': r, 't_prompt': t, 'raw_manifest': init.manifest}, f)
print('  ✅ Prompts generated and cached')
"
else
    echo "  ✅ Using cached prompts"
fi

# ── 3. Clear old telemetry ──────────────────────────────────────────────────
echo ""
echo "🧹 Step 3: Clearing old telemetry logs..."
rm -f /tmp/agent_tool_calls.log /tmp/agent_heartbeat.log
echo "  ✅ Telemetry cleared"

# ── 4. Start the agent ──────────────────────────────────────────────────────
echo ""
echo "🚀 Step 4: Starting VoiceAgentV5 benchmark agent..."
cd "$V3_DIR"
python fdb_agent.py start &
AGENT_PID=$!
echo "  Agent PID: $AGENT_PID"

# Wait for agent to be ready
sleep 5
echo "  ✅ Agent started"

# ── 5. Run inference ────────────────────────────────────────────────────────
echo ""
echo "🎙️ Step 5: Running FDB-v3 inference (100 scenarios)..."
echo "  This may take 30-60 minutes depending on model speed."

FORCE_ARG=""
if [ "$FORCE_FLAG" = "--force" ]; then
    FORCE_ARG="--force"
fi

# Set TMPDIR so NeMo doesn't unpack its 2.5GB model into the /tmp RAM disk
export TMPDIR=$(realpath ../../tmp_pip)

python run_tool_benchmark_all_released.py --provider "$PROVIDER" $FORCE_ARG
# ── 6. Stop the agent ──────────────────────────────────────────────────────
echo ""
echo "🛑 Step 6: Stopping agent..."
kill $AGENT_PID 2>/dev/null || true
wait $AGENT_PID 2>/dev/null || true
echo "  ✅ Agent stopped"

# ── 7. Run evaluation ──────────────────────────────────────────────────────
echo ""
echo "📊 Step 7: Running evaluation..."

echo "  7a. Tool accuracy (F1)..."
python evaluate_tool_calls.py \
    --benchmark benchmark_data_v2.json \
    --results-dir fdb_v3_data_released \
    --provider "$PROVIDER" \
    --output "${PROVIDER}_evaluation_report.json" \
    --use-llm || echo "  ⚠️  Tool evaluation failed (might need OPENAI_API_KEY for LLM judge)"

echo ""
echo "  7b. Pass rate..."
python evaluate_pass_rate.py \
    --benchmark benchmark_data_v2.json \
    --results-dir fdb_v3_data_released \
    --provider "$PROVIDER" \
    --output "${PROVIDER}_pass_rate_report.json" \
    --use-llm || echo "  ⚠️  Pass rate evaluation failed"

echo ""
echo "  7c. Latency analysis..."
python analyze_tool_latency.py \
    --results-dir fdb_v3_data_released \
    --provider "$PROVIDER" || echo "  ⚠️  Latency analysis failed"

# ── 8. Summary ──────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Benchmark run complete!"
echo ""
echo "  Results:"
echo "    Tool accuracy:  ${PROVIDER}_evaluation_report.json"
echo "    Pass rate:      ${PROVIDER}_pass_rate_report.json"
echo "    Latency:        ${PROVIDER}_latency_report.json"
echo "═══════════════════════════════════════════════════════════════"
