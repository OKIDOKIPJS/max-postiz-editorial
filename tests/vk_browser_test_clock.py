"""Explicit clock isolation for VK semantic browser fixtures."""

import pytest

from max_postiz_agent import vk_browser_worker as worker


@pytest.fixture
def semantic_fence_clock(monkeypatch):
    """Keep fake SQL authorization time independent of fixture host latency.

    Only semantic tests opt in. Production's default clock and the dedicated
    real authorization-expiry and native-action timing tests remain unchanged.
    Native actions still receive the worker's real timeout of at most 1000 ms;
    readiness, decode, result and readback timers also remain real.
    """
    original_mutate = worker._mutate

    def semantic_mutation(*args, **kwargs):
        return original_mutate(*args, **kwargs, _clock=lambda: 0.0)

    monkeypatch.setattr(worker, "_mutate", semantic_mutation)
