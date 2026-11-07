"""Retry only transient model requests; game writes never pass through this wrapper."""
import aiohttp


class ModelUnavailable(RuntimeError):
    pass


def request_model(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        transient = isinstance(exc, (TimeoutError, ConnectionError, aiohttp.ClientConnectionError))
        if isinstance(exc, (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError)):
            transient = False
        if isinstance(exc, aiohttp.ClientResponseError):
            transient = exc.status in (408, 429, 500, 502, 503, 504)
        if not transient:
            raise
        # Keep provider URLs, request headers and credentials out of persisted error details.
        raise ModelUnavailable('模型接口暂时不可用') from exc
