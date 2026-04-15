import random
import time
import ccxt

def retry_call(func, *args, retries=3, base_delay=1.0, max_delay=8.0, logger=None, context="", exceptions=(Exception,), **kwargs):
    """Retry a callable with exponential backoff.

    Fails fast on non-transient HTTP 4xx errors (e.g., Auth, Bad Request, Insufficient Funds).
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
                
            if logger:
                msg = f"{context} failed (attempt {attempt}/{retries}): {exc}" if context else f"Attempt {attempt}/{retries} failed: {exc}"
                logger.warning(msg)
            if attempt == retries:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1))) + random.uniform(0, 0.25)
            time.sleep(delay)
    raise last_error
