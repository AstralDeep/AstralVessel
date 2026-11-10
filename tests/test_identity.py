# Tests for verified-token callers: issuer, audience, expiry, authorized party and algorithm checks, owners keyed
# by issuer and subject, per-issuer agent grants, and per-call tool narrowing by scope.

from __future__ import annotations

import hashlib
import json
import time

import httpx
import jwt
import pytest
from conftest import APP_KEY, text_reply
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from astralvessel.client import VesselClient
from astralvessel.deployment import load_deployment
from astralvessel.server import Settings, app_keys, create_app

ISS_A, ISS_B = "https://iam.a.test/realms/Astral", "https://iam.b.test/realms/Other"


def jwk(private, kid):
    public = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True) if isinstance(
        private, rsa.RSAPrivateKey) else jwt.algorithms.OKPAlgorithm.to_jwk(private.public_key(), as_dict=True)
    return {**public, "kid": kid}


@pytest.fixture
def keys():
    return {"a": rsa.generate_private_key(public_exponent=65537, key_size=2048),
            "b": ed25519.Ed25519PrivateKey.generate(), "rogue": rsa.generate_private_key(public_exponent=65537,
                                                                                          key_size=2048)}


def token(private, kid, **claims):
    now = int(time.time())
    body = {"iss": ISS_A, "aud": "vessel-research", "sub": "user-1", "iat": now, "exp": now + 300,
            "azp": "astral-orchestrator", **claims}
    alg = "EdDSA" if isinstance(private, ed25519.Ed25519PrivateKey) else "RS256"
    return jwt.encode(body, private, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def vessel(package_dir, model, tmp_path, keys):
    (tmp_path / "a.json").write_text(json.dumps({"keys": [jwk(keys["a"], "a1")]}))
    (tmp_path / "b.json").write_text(json.dumps({"keys": [jwk(keys["b"], "b1")]}))
    path = tmp_path / "deployment.toml"
    path.write_text(f"""
[[agents]]
id = "tester"
package = "{package_dir}"

[[apps]]
name = "web"
key_sha256 = "{hashlib.sha256(APP_KEY.encode()).hexdigest()}"
agents = ["tester"]

[[issuers]]
id = "astral"
issuer = "{ISS_A}"
audience = "vessel-research"
jwks_file = "a.json"
authorized_parties = ["astral-orchestrator"]
agents = ["tester"]

[[issuers]]
id = "other"
issuer = "{ISS_B}"
audience = "vessel-research"
jwks_file = "b.json"
algorithms = ["EdDSA"]
agents = []
""")
    dep = load_deployment(path)
    settings = Settings(dep, task_db=str(tmp_path / "t.sqlite"), app_keys=app_keys(dep), allow_in_process=True)
    app = create_app(settings, model_transport=model.transport())
    return httpx.ASGITransport(app=app)


def client(transport, bearer):
    return VesselClient("http://vessel.test", bearer, transport=transport)


async def turn(c, user="ignored"):
    return [e async for e in c.stream("tester", user, "hello")]


async def test_a_valid_token_is_an_owner_keyed_by_issuer_and_subject(vessel, keys, model):
    model.replies = [text_reply("hi")]
    events = await turn(client(vessel, token(keys["a"], "a1")))
    assert events[-1]["state"] == "TASK_STATE_COMPLETED"
    task_id = events[0]["task_id"]
    same_sub_by_app = client(vessel, APP_KEY)
    assert "error" in await same_sub_by_app.rpc("tester", "user-1", "GetTask", {"id": task_id})
    assert "result" in await client(vessel, token(keys["a"], "a1")).rpc("tester", "x", "GetTask", {"id": task_id})


async def test_bad_tokens_are_refused(vessel, keys, model):
    now = int(time.time())
    hs256 = jwt.encode({"iss": ISS_A, "aud": "vessel-research", "sub": "u", "iat": now, "exp": now + 60},
                       "a-shared-secret-of-sufficient-length", algorithm="HS256", headers={"kid": "a1"})
    unsigned = jwt.encode({"iss": ISS_A, "aud": "vessel-research", "sub": "u", "iat": now, "exp": now + 60},
                          None, algorithm="none")
    bad = {
        "wrong audience": token(keys["a"], "a1", aud="someone-else"),
        "expired": token(keys["a"], "a1", iat=now - 900, exp=now - 600),
        "unknown issuer": token(keys["a"], "a1", iss="https://evil.test"),
        "forged signature": token(keys["rogue"], "a1"),
        "unknown key": token(keys["a"], "zz"),
        "wrong client": token(keys["a"], "a1", azp="some-other-client"),
        "shared secret": hs256,
        "unsigned": unsigned,
        "no subject": token(keys["a"], "a1", sub=""),
    }
    for name, value in bad.items():
        events = await turn(client(vessel, value))
        assert events[0]["type"] == "error" and events[0]["status"] == 401, name
    assert model.requests == []


async def test_issuers_get_only_their_agents(vessel, keys):
    other = token(keys["b"], "b1", iss=ISS_B, azp=None)
    events = await turn(client(vessel, other))
    assert events[0]["status"] == 403
    assert await client(vessel, other).agents("x") == []


async def test_scopes_narrow_the_tools_a_turn_may_use(vessel, keys, model):
    model.replies = [text_reply("a"), text_reply("b")]
    await turn(client(vessel, token(keys["a"], "a1", scope="openid tools:read")))
    offered = {t["function"]["name"] for t in model.requests[0]["tools"]}
    assert offered == {"echo", "present"}
    c = client(vessel, APP_KEY)
    c._client.headers["X-Vessel-Scopes"] = "tools:files tools:search"
    await turn(c, "alice")
    assert {t["function"]["name"] for t in model.requests[1]["tools"]} == {"read", "grep", "glob", "list"}


def test_issuer_settings_are_checked(package_dir, tmp_path):
    base = f'[[agents]]\nid = "tester"\npackage = "{package_dir}"\n'
    for issuer in ('id = "x"\nissuer = "i"\naudience = "a"\njwks_file = "k.json"\nalgorithms = ["HS256"]\nagents = []',
                   'id = "x"\nissuer = "i"\naudience = "a"\nagents = []',
                   'id = "x"\nissuer = "i"\naudience = "a"\njwks_url = "http://keys.test"\nagents = []'):
        (tmp_path / "d.toml").write_text(base + "[[issuers]]\n" + issuer + "\n")
        with pytest.raises(ValueError):
            load_deployment(tmp_path / "d.toml")
