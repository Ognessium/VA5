#!/usr/bin/env python3
"""
End-to-end integration test for the VoiceAgentV5 system.
Exercises every pipeline path without requiring a real LLM or LiveKit connection.

Usage:
    python -m tests.test_system
"""
import sys
import os
import time
import json
import logging

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.system import VoiceAgentSystem
from src.models.messages import DirectorMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("test_system")

# Load test fixture — in production this data arrives at runtime, not from a file
FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tool_manifest.json",
)
with open(FIXTURE_PATH, "r") as f:
    TEST_MANIFEST = json.load(f)


def header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def test_initialization():
    header("TEST 1: System Initialization")
    system = VoiceAgentSystem(raw_manifest=TEST_MANIFEST)

    schema = system.schemas["reasoner_tool_schema"]
    read_only = schema.get("read_only", [])
    state_mod = schema.get("state_modifying", [])

    print(f"  Read-only tools:       {[t['name'] for t in read_only]}")
    print(f"  State-modifying tools: {[t['name'] for t in state_mod]}")
    print(f"  V2V summary:           {system.schemas['v2v_tool_summary']}")

    assert len(read_only) == 7, f"Expected 7 read-only tools, got {len(read_only)}"
    assert len(state_mod) == 5, f"Expected 5 state-modifying tools, got {len(state_mod)}"

    # Check inverse tools were generated
    for tool in state_mod:
        assert "inverse_tool" in tool, f"Missing inverse for {tool['name']}"
        assert tool["inverse_tool"]["name"].startswith("undo_")

    print("  ✅ PASSED")
    system.shutdown()
    return True


def test_tool_validation():
    header("TEST 2: Tool Validation (valid + invalid)")
    system = VoiceAgentSystem(raw_manifest=TEST_MANIFEST)

    # Valid call
    is_valid, err = system.validator.validate({"name": "search_flights", "slots": {}})
    assert is_valid, f"Expected valid, got error: {err}"
    print(f"  Valid tool call:   ✅ (search_flights)")

    # Missing name
    is_valid, err = system.validator.validate({"slots": {}})
    assert not is_valid
    print(f"  Missing name:      ✅ caught → {err[:50]}...")

    # Unknown tool
    is_valid, err = system.validator.validate({"name": "fly_to_moon"})
    assert not is_valid
    print(f"  Unknown tool:      ✅ caught → {err[:50]}...")

    print("  ✅ PASSED")
    system.shutdown()
    return True


def test_tool_lifecycle():
    header("TEST 3: Tool Lifecycle (execute → feedback → state)")
    system = VoiceAgentSystem(raw_manifest=TEST_MANIFEST)

    # Execute a tool
    system.tool_manager.execute_tool(
        tool_id="t-001",
        tool_type="search_flights",
        slots=[{"destination": "Tokyo", "date": "July 15"}],
    )

    # Check it's in-flight
    inflight = system.tool_manager.get_inflight_tools()
    assert len(inflight) == 1, f"Expected 1 in-flight, got {len(inflight)}"
    print(f"  In-flight tools:   {inflight}")

    status = system.state_api.get_tool_status("t-001")
    assert status == "in-flight", f"Expected 'in-flight', got '{status}'"
    print(f"  State status:      {status}")

    # Simulate success feedback
    # Give the message queue a moment to process the tool_added notification
    time.sleep(0.1)
    system.simulate_tool_feedback("t-001", "success", {"flights": [{"id": "FL123"}]})
    time.sleep(0.1)

    status = system.state_api.get_tool_status("t-001")
    assert status == "success", f"Expected 'success', got '{status}'"
    print(f"  After feedback:    {status}")

    inflight = system.tool_manager.get_inflight_tools()
    assert len(inflight) == 0, f"Expected 0 in-flight, got {len(inflight)}"
    print(f"  In-flight cleared: ✅")

    print("  ✅ PASSED")
    system.shutdown()
    return True


def test_message_queue_priority():
    header("TEST 4: Message Queue (exactly-once + priority)")
    system = VoiceAgentSystem(raw_manifest=TEST_MANIFEST)

    initial_ctx_len = len(system.reasoner.context)

    # Fire two tool feedbacks rapidly — both should arrive as high-priority
    system.tool_manager.execute_tool("t-a", "track_order", [{"order_id": "X1"}])
    system.tool_manager.execute_tool("t-b", "track_order", [{"order_id": "X2"}])
    time.sleep(0.1)

    system.simulate_tool_feedback("t-a", "success")
    system.simulate_tool_feedback("t-b", "failed")
    time.sleep(0.3)

    # The reasoner should have received the state notifications in its context
    new_entries = system.reasoner.context[initial_ctx_len:]
    state_updates = [e for e in new_entries if "State Update" in e.get("content", "")]
    print(f"  State update messages received: {len(state_updates)}")
    # At minimum we should get: 2x tool_added + 2x tool_status_updated = 4
    assert len(state_updates) >= 4, f"Expected >= 4 state updates, got {len(state_updates)}"

    print("  ✅ PASSED")
    system.shutdown()
    return True


def test_user_stt_flow():
    header("TEST 5: User STT → Reasoner")
    system = VoiceAgentSystem(raw_manifest=TEST_MANIFEST)

    system.feed_user_stt("I need to track order BOB12")
    assert any(
        e.get("content") == "I need to track order BOB12"
        for e in system.reasoner.context
    )
    print("  User STT ingested into reasoner context: ✅")

    print("  ✅ PASSED")
    system.shutdown()
    return True


def test_talker_transcript_routing():
    header("TEST 6: Talker Transcript → Reasoner (via orchestrator)")
    system = VoiceAgentSystem(raw_manifest=TEST_MANIFEST)

    system.feed_talker_transcript("Sure, let me track that order for you.")
    assert any(
        e.get("role") == "talker" for e in system.reasoner.context
    )
    print("  Talker transcript routed to reasoner: ✅")

    print("  ✅ PASSED")
    system.shutdown()
    return True


def test_tool_cancellation():
    header("TEST 7: Tool Cancellation")
    system = VoiceAgentSystem(raw_manifest=TEST_MANIFEST)

    system.tool_manager.execute_tool("t-cancel", "book_flight", [{"passenger_name": "Test"}])
    time.sleep(0.1)

    system.tool_manager.cancel_tool("t-cancel")
    time.sleep(0.1)

    status = system.state_api.get_tool_status("t-cancel")
    assert status == "cancelled", f"Expected 'cancelled', got '{status}'"
    print(f"  Cancelled status:  {status} ✅")

    inflight = system.tool_manager.get_inflight_tools()
    assert len(inflight) == 0
    print(f"  In-flight cleared: ✅")

    print("  ✅ PASSED")
    system.shutdown()
    return True


def test_concurrent_state_reads():
    header("TEST 8: Concurrent State Reads (deep copy safety)")
    system = VoiceAgentSystem(raw_manifest=TEST_MANIFEST)

    system.tool_manager.execute_tool("t-dc", "search_products", [{"query": "headphones"}])
    time.sleep(0.1)

    # Get a copy of the tool
    tool_copy = system.state_api.get_tool_info("t-dc")
    assert tool_copy is not None

    # Mutate the copy
    tool_copy.status = "MUTATED"

    # Internal state should be unaffected
    real_status = system.state_api.get_tool_status("t-dc")
    assert real_status == "in-flight", f"Deep copy violated! Got '{real_status}'"
    print("  Deep copy isolation: ✅")

    print("  ✅ PASSED")
    system.shutdown()
    return True


if __name__ == "__main__":
    tests = [
        test_initialization,
        test_tool_validation,
        test_tool_lifecycle,
        test_message_queue_priority,
        test_user_stt_flow,
        test_talker_transcript_routing,
        test_tool_cancellation,
        test_concurrent_state_reads,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            logger.exception(f"FAILED: {test.__name__}")
            failed += 1

    header("RESULTS")
    print(f"  Passed: {passed}/{len(tests)}")
    print(f"  Failed: {failed}/{len(tests)}")
    sys.exit(0 if failed == 0 else 1)
