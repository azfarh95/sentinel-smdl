"""GPU gates — broker status gate + VRAM-headroom preflight (mocked I/O)."""
from app import broker, gpu


def test_broker_gate_disabled_fails_open(monkeypatch):
    monkeypatch.setattr(broker.config, "GPU_BROKER_ENABLED", False)
    ok, reason = broker.gpu_gate()
    assert ok and "fail-open" in reason


def test_broker_gate_defers_when_qwen_blocked(monkeypatch):
    monkeypatch.setattr(broker.config, "GPU_BROKER_ENABLED", True)
    monkeypatch.setattr(broker, "status", lambda: {
        "qwen_allowed": False, "flux_active": True, "reason": "auto/one_at_a_time"})
    ok, reason = broker.gpu_gate()
    assert not ok and "flux" in reason.lower()


def test_broker_gate_allows_when_free(monkeypatch):
    monkeypatch.setattr(broker.config, "GPU_BROKER_ENABLED", True)
    monkeypatch.setattr(broker, "status", lambda: {"qwen_allowed": True, "reason": "free"})
    ok, _ = broker.gpu_gate()
    assert ok


def test_vram_gate_skipped_when_unavailable(monkeypatch):
    monkeypatch.setattr(gpu, "free_vram_gb", lambda: None)
    ok, reason = gpu.vram_gate()
    assert ok and "skipped" in reason


def test_vram_gate_defers_when_low(monkeypatch):
    monkeypatch.setattr(gpu, "free_vram_gb", lambda: 11.5)
    monkeypatch.setattr(gpu.config, "MIN_VRAM_GB", 17.0)
    ok, reason = gpu.vram_gate()
    assert not ok and "11.5" in reason


def test_vram_gate_allows_with_headroom(monkeypatch):
    monkeypatch.setattr(gpu, "free_vram_gb", lambda: 24.0)
    monkeypatch.setattr(gpu.config, "MIN_VRAM_GB", 17.0)
    ok, _ = gpu.vram_gate()
    assert ok
