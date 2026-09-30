# ── FDB-v3 Agent Launcher ────────────────────────────────────────────────────
# Uncomment the agent you want to run for the benchmark.

# VoiceAgentV5 — Dual-Architecture (Talker + Reasoner)
python fdb_agent.py start

# Reference agents (for comparison):
# LK_PROVIDER=gemini3_1 python lk_agent_tool.py start
# LK_PROVIDER=gemini2_5 python lk_agent_tool.py start
# LK_PROVIDER=gpt_realtime python lk_agent_tool.py start
# python cascaded_agent.py start
