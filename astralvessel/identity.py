# Verifies signed caller tokens from the issuers a deployment trusts: issuer, audience, expiry, authorized party
# and an asymmetric signature from the issuer's published keys. Shared-secret and unsigned tokens never pass.

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass

import httpx
import jwt

from .deployment import IssuerEntry

log = logging.getLogger("astralvessel")
SUBJECT = re.compile(r"^[\x21-\x7e]{1,200}$")
REFRESH_SECONDS = 60


@dataclass(frozen=True)
class Caller:
    issuer: IssuerEntry
    subject: str
    scopes: frozenset[str]

    @property
    def owner(self) -> str:
        return f"{self.issuer.id}|{self.subject}"


class KeySet:
    # The issuer's signing keys, read from a file or fetched from its JWKS URL (through the egress proxy) and
    # refreshed at most once a minute when a token names a key we do not have.
    def __init__(self, issuer: IssuerEntry, proxy: str | None):
        self.issuer, self.proxy = issuer, proxy
        self.keys: dict[str, jwt.PyJWK] = {}
        self.fetched = 0.0
        self.lock = asyncio.Lock()

    async def key(self, kid: str | None) -> jwt.PyJWK | None:
        if kid not in self.keys and time.monotonic() - self.fetched > REFRESH_SECONDS:
            async with self.lock:
                if kid not in self.keys and time.monotonic() - self.fetched > REFRESH_SECONDS:
                    await self._load()
        if kid is None and len(self.keys) == 1:
            return next(iter(self.keys.values()))
        return self.keys.get(kid)

    async def _load(self) -> None:
        self.fetched = time.monotonic()
        try:
            if self.issuer.jwks_file:
                with open(self.issuer.jwks_file) as handle:
                    document = json.load(handle)
            else:
                async with httpx.AsyncClient(proxy=self.proxy, trust_env=False, follow_redirects=False,
                                             timeout=10) as client:
                    response = await client.get(self.issuer.jwks_url or "")
                    response.raise_for_status()
                    document = response.json()
            keys = {}
            for entry in document.get("keys", []):
                try:
                    key = jwt.PyJWK(entry)
                except jwt.PyJWTError:
                    continue
                keys[entry.get("kid")] = key
            self.keys = keys
        except Exception:  # noqa: BLE001  keep the keys we had; tokens signed with unknown keys are refused
            log.warning("could not load signing keys for issuer %s", self.issuer.id, exc_info=True)


class Verifier:
    def __init__(self, issuers: list[IssuerEntry]):
        proxy = os.environ.get("VESSEL_WEB_PROXY") or None
        self.issuers = {i.issuer: (i, KeySet(i, proxy)) for i in issuers}

    async def verify(self, token: str) -> Caller | None:
        try:
            header = jwt.get_unverified_header(token)
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return None
        found = self.issuers.get(claims.get("iss"))
        if found is None:
            return None
        issuer, keyset = found
        if header.get("alg") not in issuer.algorithms:
            return None
        key = await keyset.key(header.get("kid"))
        if key is None:
            return None
        try:
            claims = jwt.decode(token, key=key, algorithms=issuer.algorithms, audience=issuer.audience,
                                issuer=issuer.issuer, leeway=60,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError:
            return None
        subject = str(claims.get("sub", ""))
        if not SUBJECT.fullmatch(subject):
            return None
        if issuer.authorized_parties and claims.get("azp") not in issuer.authorized_parties:
            return None
        scopes = frozenset(str(claims.get("scope") or "").split())
        return Caller(issuer, subject, scopes)


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and token.startswith("ey")
