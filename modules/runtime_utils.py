import random
import time

import ccxt


_IDEMPOTENCY_CONFLICT_MARKERS = (
    "already exists",
    "duplicate",
    "orderlinkid",
    "order link id",
    "client order id",
    "conflict",
)


def _is_idempotency_conflict(exc):
    text = " ".join(str(part) for part in getattr(exc, "args", ()) if part is not None).strip().lower()
    if not text:
        text = str(exc).lower()
    return any(marker in text for marker in _IDEMPOTENCY_CONFLICT_MARKERS)


def retry_call(
    func,
    *args,
    retries=3,
    base_delay=1.0,
    max_delay=8.0,
    logger=None,
    context="",
    exceptions=(Exception,),
    idempotency_key=None,
    resolve_idempotency_conflict=None,
    **kwargs,
):
    """Retry a callable with exponential backoff.

    Fails fast on non-transient HTTP 4xx errors (e.g., Auth, Bad Request, Insufficient Funds).
    If an idempotent write hits a duplicate/link-id conflict, retrying is skipped and the
    caller can optionally resolve the already-created order via resolve_idempotency_conflict.
    Raises the last exception if all attempts fail.
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except exceptions as exc:
            last_error = exc

            # Fail fast on non-transient ccxt errors
            if isinstance(exc, (ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.BadRequest, ccxt.InsufficientFunds)):
                if logger:
                    logger.error(f"{context} FATAL non-transient error: {exc}")
                raise exc

            if idempotency_key is not None and _is_idempotency_conflict(exc):
                if logger:
                    logger.warning(f"{context} idempotency conflict for {idempotency_key}: {exc}")
                if callable(resolve_idempotency_conflict):
                    resolved = resolve_idempotency_conflict()
                    if resolved is not None:
                        if logger:
                            logger.info(f"{context} resolved existing result for {idempotency_key}")
                        return resolved
                raise exc

            if logger:
                msg = f"{context} failed (attempt {attempt}/{retries}): {exc}" if context else f"Attempt {attempt}/{retries} failed: {exc}"
                logger.warning(msg)
            if attempt == retries:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1))) + random.uniform(0, 0.25)
            time.sleep(delay)
    raise last_error
