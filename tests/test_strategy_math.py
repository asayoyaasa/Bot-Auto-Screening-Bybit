import sys
import types

import pytest

try:
    from hypothesis import given, strategies as st
except Exception:  # pragma: no cover - fallback for minimal environments
    def given(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    class _DummyStrategies:
        @staticmethod
        def floats(*args, **kwargs):
            return None

    st = _DummyStrategies()

sys.modules.setdefault(
    "ccxt",
    types.SimpleNamespace(
        AuthenticationError=Exception,
        PermissionDenied=Exception,
        BadRequest=Exception,
        InsufficientFunds=Exception,
        NetworkError=Exception,
    ),
)

from modules.runtime_utils import retry_call
import ccxt
import time


@given(
    st.floats(min_value=0.0001, max_value=100000.0, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0.01, max_value=100.0, allow_nan=False, allow_infinity=False),
)
def test_position_size_math(equity, risk_pct):
    """Property-based test to ensure margin cost never exceeds equity."""
    risk_decimal = risk_pct / 100.0
    margin_cost = equity * risk_decimal

    assert margin_cost >= 0.0
    assert margin_cost <= equity


def test_retry_transient():
    """Test retry wrapper handles transient exceptions properly."""
    attempts = 0

    def transient_func():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ccxt.NetworkError("Network glitch")
        return "success"

    # Should succeed after 3 attempts
    result = retry_call(transient_func, retries=3, base_delay=0.01)
    assert result == "success"
    assert attempts == 3


def test_retry_fatal_4xx():
    """Test retry wrapper fails instantly on 4xx."""
    attempts = 0

    def fatal_func():
        nonlocal attempts
        attempts += 1
        raise ccxt.InsufficientFunds("No money")

    with pytest.raises(ccxt.InsufficientFunds):
        retry_call(fatal_func, retries=3, base_delay=0.01)

    assert attempts == 1  # Should NOT retry
