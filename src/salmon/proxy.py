"""Send a service's HTTP requests through the proxy the config gives it ([proxy] in config.default.toml).

Opt-in: a service without a proxy gets the same connector and session as it would without this module.
"""

import asyncio
import ssl
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp_socks import ProxyConnectionError, ProxyConnector, ProxyError, ProxyTimeoutError

if TYPE_CHECKING:
    from aiohttp import ClientRequest, ClientTimeout
    from aiohttp.client_proto import ResponseHandler

# The proxy URL schemes salmon takes, each mapped to the one aiohttp_socks knows it by. The proxy
# always resolves hostnames, so the name of the site a request goes to is not looked up locally:
# socks4 works as SOCKS4A, and socks5 as socks5h.
SCHEMES = {"http": "http", "socks4": "socks4", "socks4a": "socks4", "socks5": "socks5", "socks5h": "socks5"}


def is_valid_url(url: str) -> bool:
    """Whether `url` is a proxy URL salmon can use: scheme://[user:password@]host:port."""
    try:
        parts = urlsplit(url)
        return parts.scheme in SCHEMES and bool(parts.hostname) and parts.port is not None
    except ValueError:  # An invalid port or IPv6 address
        return False


def proxy_url(service: str) -> str | None:
    """Get the URL of the proxy for `service`, or None to connect directly.

    The service's own setting wins, and an empty one connects it directly; else [proxy] url applies.

    Args:
        service: The service's key in [proxy.services].
    """
    # Not at import time: salmon.config imports this module to validate the config cfg is built from.
    from salmon import cfg

    url = getattr(cfg.proxy.services, service)
    if url is None:
        url = cfg.proxy.url
    return url or None


def connector(service: str, **kwargs: Any) -> aiohttp.TCPConnector:
    """Make a connector for `service`'s requests: through its proxy, else a direct TCPConnector.

    Args:
        service: The service's key in [proxy.services].
        **kwargs: TCPConnector arguments, such as limit, kept with a proxy too.
    """
    url = proxy_url(service)
    return aiohttp.TCPConnector(**kwargs) if url is None else _proxy_connector(url, **kwargs)


def session_kwargs(service: str) -> dict[str, Any]:
    """Get the ClientSession arguments sending `service`'s requests through its proxy: none without one."""
    url = proxy_url(service)
    return {} if url is None else {"connector": _proxy_connector(url)}


def _proxy_connector(url: str, **kwargs: Any) -> ProxyConnector:
    parts = urlsplit(url)
    url = parts._replace(scheme=SCHEMES[parts.scheme]).geturl()
    return _ProxyConnector.from_url(url, rdns=True, **kwargs)


class _ProxyConnector(ProxyConnector):
    """A ProxyConnector failing with aiohttp's errors, as a direct connection does.

    aiohttp_socks raises errors of its own, and lets errors from the handshake with the site
    through as they are: none of them is an aiohttp.ClientError, so code catching aiohttp's errors
    would miss them. They all happen before the request is written, so each becomes the error a
    direct connection failing at that step raises: a timeout, or a ClientConnectorError, which the
    tracker client retries as a request the tracker never got.
    """

    async def _wrap_create_connection(
        self,
        *args: Any,
        addr_infos: Any,
        req: "ClientRequest",
        timeout: "ClientTimeout",
        client_error: type[Exception] = aiohttp.ClientConnectorError,
        **kwargs: Any,
    ) -> tuple[asyncio.Transport, "ResponseHandler"]:
        try:
            return await super()._wrap_create_connection(
                *args, addr_infos=addr_infos, req=req, timeout=timeout, client_error=client_error, **kwargs
            )
        except TimeoutError:
            raise
        except ProxyTimeoutError as exc:
            raise TimeoutError(str(exc)) from exc
        except ssl.CertificateError as exc:
            raise aiohttp.ClientConnectorCertificateError(req.connection_key, exc) from exc
        except ssl.SSLError as exc:
            raise aiohttp.ClientConnectorSSLError(req.connection_key, exc) from exc
        except (ProxyConnectionError, ProxyError, OSError, EOFError) as exc:
            raise aiohttp.ClientProxyConnectionError(req.connection_key, OSError(None, f"proxy: {exc}")) from exc
