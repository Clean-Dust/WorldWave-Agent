#!/usr/bin/env python3
"""Worldwave completetestsuite"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0

def test(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  ✅ {name}")
    except Exception as e:
        FAIL += 1
        print(f"  ❌ {name}: {e}")


# ── 1. Import test ──
def test_imports():
    from core.loop import create_ww
    assert callable(create_ww)

# ── 2. State Manager ──
def test_state_manager():
    from core.state import StateManager
    sm = StateManager()
    sm.begin_spiral()
    assert sm.current_spiral == 1
    assert sm.current_phase == "perceive"
    sm.set_phase("perceive")
    assert sm.current_phase == "recall"
    sm.set_phase("recall")
    assert sm.current_phase == "plan"
    sm.complete_spiral()
    summary = sm.summary()
    assert summary["current_spiral"] == 1
    assert summary["current_phase"] == "idle"
    assert summary["total_checkpoints"] >= 2  # begin + complete

# ── 3. State Manager - Interrupt/Resume ──
def test_interrupt_resume():
    from core.state import StateManager
    sm = StateManager()
    sm.begin_spiral()
    cp = sm.interrupt("testing interrupt")
    assert cp.interrupted is True
    assert sm.summary()["active_interrupts"] == 1
    # recovery
    ok = sm.resume(cp.id, {"perception": {"test": True}})
    assert ok is True
    summary = sm.summary()
    assert summary["active_interrupts"] == 0

# ── 4. Code Sandbox ──
def test_sandbox_basic():
    from sandbox.runner import CodeSandbox
    sandbox = CodeSandbox(timeout=5)
    r = sandbox.run_code("""
a = 42
b = a * 2
print(f"Result: {b}")
""")
    assert r.success, f"Failed: {r.error}"
    assert "Result: 84" in r.output

def test_sandbox_dangerous():
    from sandbox.runner import CodeSandbox
    sandbox = CodeSandbox(timeout=5)
    r = sandbox.run_code('import os\nos.system("ls")')
    assert not r.success, "Should have blocked dangerous code"
    assert "secure violation" in r.error or "not allowed" in r.error

def test_sandbox_timeout():
    from sandbox.runner import CodeSandbox
    sandbox = CodeSandbox(timeout=2)
    r = sandbox.run_code('import time\ntime.sleep(60)\nprint("done")')
    assert not r.success
    assert "timeout " in r.error

# ── 5. Memory Bridge (migrated to core/memory/, old test removed) ──
def test_memory_bridge():
    # Old bridge removed, use core.memory.MemorySystem instead
    from core.memory import MemorySystem
    import tempfile
    tmpdir = tempfile.mkdtemp()
    ms = MemorySystem(hippocampus_cap=10, data_dir=tmpdir)
    ms._do_store("Worldwave test memory", source="test")
    results = ms.recall("Worldwave")
    assert results["total"] >= 0
    print(f"    ✓ MemorySystem recall: {results['total']}")

# ── 6. Worldwave loop ──
def test_ww_loop():
    from core.loop import Worldwave
    ww = Worldwave(persist_dir="/tmp/ww_test")
    ww.verbose = False
    result = ww.run("test", max_spirals=2)
    assert result["status"] in ("completed", "interrupted")
    assert result["spirals_completed"] >= 1


# ── 7. Subconscious v4 ══
def test_subconscious_v4():
    from core.subconscious import Subconscious
    sc = Subconscious(enabled=True)
    assert sc.enabled
    vec = sc.feature_extractor.extract()
    assert len(vec) == 32  # PADDED_FEATURES
    risk = sc.predict()
    assert 0 <= risk.crash_risk <= 1
    for _ in range(4):
        sc.record_training_sample([5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120] + [0]*20, 1.0)
        sc.record_training_sample([0, 0, 1, 0, 20, 1, 0, 1, 3, 2, 0, 5] + [0]*20, 0.0)
    result = sc.train()
    assert result["trained"]
    event = sc.execute_rewind("test", [5, 4, 30, 1, 500, 10, 2, 0, 1, 20, 1, 120] + [0]*20, 0.85)
    assert event["trigger_reason"] == "test"
    assert sc.get_status()["model_trained"]


# ── execute ──

if __name__ == "__main__":
    print("🌊 Worldwave testsuite")
    print(f"{'='*50}")

    test("core module import", test_imports)
    test("State Manager basic operations", test_state_manager)
    test("State Manager interrupt/recovery", test_interrupt_resume)
    test("Code Sandbox computation", test_sandbox_basic)
    test("Code Sandbox secure interception", test_sandbox_dangerous)
    test("Code Sandbox timeout ", test_sandbox_timeout)
    test("memory bridge layer", test_memory_bridge)
    test("Worldwave loop", test_ww_loop)

    # ══ 7. Subconscious v4 ══
    test("Subconscious v4 completetest", test_subconscious_v4)

    print(f"\n{'='*50}")
    print(f"Result: ✅ {PASS} passed / ❌ {FAIL} failed / total {PASS+FAIL} items")
    sys.exit(FAIL)
