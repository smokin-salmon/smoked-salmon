from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp import BaseConnector


def get_connector(service: str) -> BaseConnector | None:
    """Return an aiohttp connector configured for the proxy of *service*, or None.

    Resolution order:
      1. cfg.proxy.services.<service>  – explicitly set (empty string → no proxy)
      2. cfg.proxy.url                 – global fallback
      3. None                          – direct connection
    """
    from salmon import cfg

    service_proxy = getattr(cfg.proxy.services, service, None)
    if service_proxy is not None:
        proxy_url = service_proxy or None  # empty string → explicitly disabled
    else:
        proxy_url = cfg.proxy.url or None

    if not proxy_url:
        return None

    from aiohttp_socks import ProxyConnector

    return ProxyConnector.from_url(proxy_url)


def session_kwargs(service: str) -> dict[str, Any]:
    """Return keyword args for aiohttp.ClientSession to route *service* through its proxy."""
    connector = get_connector(service)
    return {"connector": connector} if connector is not None else {}
