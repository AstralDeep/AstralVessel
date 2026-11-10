# Calls application-provided services for an agent's service tools: the vessel holds each service's URL and
# credentials, so the sandbox never gets network access or a secret, and the model controls only the request body.

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .deployment import ServiceGrant, check_service_url, resolve
from .manifest import DeclaredTool

log = logging.getLogger("astralvessel")
MAX_RESPONSE_BYTES = 1_000_000


class ServiceClient:
    def __init__(self, agent_id: str, grants: dict[str, ServiceGrant],
                 transport: httpx.AsyncBaseTransport | None = None):
        self.agent_id = agent_id
        self.endpoints = {name: (check_service_url(resolve(g.url)).rstrip("/"),
                                 {k: resolve(v) for k, v in g.headers.items()})
                          for name, g in grants.items()}
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)

    async def call(self, tool: DeclaredTool, args: dict[str, Any], owner: str, timeout: int) -> tuple[str, bool]:
        base, headers = self.endpoints[tool.service or ""]
        context = {"X-Vessel-Owner": owner, "X-Vessel-Agent": self.agent_id, "X-Vessel-Tool": tool.name}
        try:
            async with self._client.stream("POST", base + (tool.path or ""), json=args, timeout=timeout,
                                           headers={**headers, **context}) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        return f"{tool.name}: the service's answer was too large", True
        except httpx.TimeoutException:
            return f"{tool.name}: the service did not answer within {timeout} s", True
        except httpx.HTTPError:
            log.warning("service %s unreachable for tool %s", tool.service, tool.name, exc_info=True)
            return f"{tool.name}: the service is unreachable", True
        text = body.decode("utf-8", "replace")
        for secret in headers.values():
            if len(secret) >= 8:
                text = text.replace(secret, "[redacted]")  # a service that echoes its credentials must not leak them
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            return f"{tool.name} failed (HTTP {response.status_code})", True
        if "json" in response.headers.get("content-type", ""):
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
            except ValueError:
                pass
        # A 4xx answer is the application explaining a bad request to the model; pass its words on as an error.
        return text or f"[HTTP {response.status_code}, empty]", response.status_code >= 400

    async def close(self) -> None:
        await self._client.aclose()
