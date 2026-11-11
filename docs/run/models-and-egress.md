# Models and egress

The egress container is a deployment's only way out. It holds the model key, forwards model calls from the vessel,
and runs the guarded web proxy the web tools use. This page covers choosing a model endpoint and what the egress
lets through.

## Choose a model endpoint

Any OpenAI-compatible chat completions endpoint works. Set its base URL, including the version path, and its key:

```toml
[egress]
upstream_url = "https://api.example.com/v1"
upstream_key = "env:VESSEL_UPSTREAM_MODEL_KEY"
```

`upstream_url` is required and must start with `http://`, `https://` or `env:`. Each agent then picks a model name:

```toml
# vessel.toml
[model]
model = "your-model-name"
```

The egress forwards to `<upstream_url>/chat/completions`. The endpoint must support streaming (`"stream": true`)
with `stream_options.include_usage`, and tool calls in the OpenAI format if the agent has tools.

## How a model call travels

1. The vessel calls `http://egress:8080/v1/chat/completions` with a token derived at deploy time from
   `VESSEL_SANDBOX_TOKEN`. It never holds the real key.
2. The egress checks the token, replaces it with the real key, and forwards the request to `upstream_url`.
3. The response streams back unchanged.

The egress forwards only `POST chat/completions` and `GET models`. Any other path is refused with 404, and a caller
without the vessel's token is refused with 403, so other containers on the egress network (search, for example)
cannot spend the key.

Keep the agent's `[model] endpoint` and `api_key` at their defaults (`env:VESSEL_MODEL_URL`, `env:VESSEL_MODEL_KEY`);
the generated Compose file sets both for the egress.

## Model behavior the loop handles

| Situation | What happens |
|---|---|
| HTTP 429, 500, 502, 503 or 504 | Retried up to three times, waiting 1, 2 and 4 seconds |
| Connection failure before any data arrives | Retried the same way |
| Another error status, or a failure mid-stream | The turn ends with status `model_error` and a generic message; the response detail is logged for the operator only |
| Reasoning in `reasoning_content` or `reasoning` | Streamed as `reasoning.delta` events and kept in the transcript |
| Tool arguments split across chunks | Reassembled |
| Tool arguments that are not a JSON object | The tool is not run; the model is told to call again with valid JSON |
| No response data for 180 seconds | The read times out |

## The web proxy

`webfetch`, `websearch` (with DuckDuckGo), SearXNG, and fetching token issuers' signing keys go through the egress's
forward proxy on port 3128. It allows:

- `CONNECT` to port 443, and plain `GET` or `HEAD` to port 80;
- only hosts whose every resolved address is globally routable. Private, loopback, link-local, multicast, reserved,
  shared (`100.64.0.0/10`) and cloud metadata addresses are refused, as are `localhost` and names ending in
  `.local`, `.internal`, `.localhost` or `.arpa`.

It connects to the address it checked, so a DNS answer that changes between check and connect cannot redirect it.
Each tunnel is closed after 16 MB or 60 seconds of silence.

The model endpoint itself is the operator's choice and is not subject to these checks.

## Check the egress

```bash
docker compose -f compose.generated.yml --env-file .env exec egress \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"
```

The health answer shows the configured endpoint and whether a key is set, never the key.
