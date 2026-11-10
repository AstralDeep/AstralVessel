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

    def _pick(self, kid: str | None) -> jwt.PyJWK | None:
        if kid is None and len(self.keys) == 1:
            return next(iter(self.keys.values()))
        return self.keys.get(kid)

    async def key(self, kid: str | None) -> jwt.PyJWK | None:
        found = self._pick(kid)
        if found is not None:
            return found
        # Requests that arrive during a fetch wait for it rather than being refused.
        async with self.lock:
            found = self._pick(kid)
            if found is None and (not self.fetched or time.monotonic() - self.fetched > REFRESH_SECONDS):
                await self._load()
                found = self._pick(kid)
        return found

    async def _load(self) -> None:
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
                # Only signing keys: an encryption key published in the same set never verifies a token.
                if not isinstance(entry, dict) or entry.get("use", "sig") != "sig" or (
                        "key_ops" in entry and "verify" not in (entry.get("key_ops") or [])):
                    continue
                try:
                    key = jwt.PyJWK(entry)
                except jwt.PyJWTError:
                    continue
                keys[entry.get("kid")] = key
            if keys:
                self.keys = keys
            else:
                log.warning("issuer %s published no usable signing keys; keeping the previous set", self.issuer.id)
        except Exception:  # noqa: BLE001  keep the keys we had; tokens signed with unknown keys are refused
            log.warning("could not load signing keys for issuer %s", self.issuer.id, exc_info=True)
        finally:
            self.fetched = time.monotonic()


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
        issuer_claim = claims.get("iss")
        found = self.issuers.get(issuer_claim) if isinstance(issuer_claim, str) else None
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
        raw = claims.get("scope") or claims.get("scp") or ""
        # OAuth puts scopes in one space-separated string; some providers send a list.
        scopes = frozenset(raw.split() if isinstance(raw, str) else (s for s in raw if isinstance(s, str))
                           if isinstance(raw, list) else ())
        return Caller(issuer, subject, scopes)


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and token.startswith("ey")
