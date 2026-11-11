# Search

The `websearch` tool gets results from one of two providers, chosen per deployment. This page compares them.

```toml
[search]
provider = "searxng"   # or "duckduckgo"
```

| | `searxng` (default) | `duckduckgo` |
|---|---|---|
| Extra container | `search`, running SearXNG `2026.10.25-54ca1d9cf` | none |
| Account or key | none | none |
| Engines | SearXNG's defaults plus Bing, Yahoo, Mojeek and Startpage, queried together | DuckDuckGo's HTML page |
| Rate limits and CAPTCHAs | One engine's block leaves the others answering; an engine that returns a CAPTCHA is suspended for 10 minutes | DuckDuckGo answers repeated queries with a bot challenge |
| Traffic | Every request through the egress proxy | Through the egress proxy |

## The SearXNG container

`deploy` writes its settings to `.generated/searxng.yml` and a secret to `.generated/search.env` (kept across
redeploys). The container is on the internal `egress` network only: the vessel can query it, no sandbox can, and
it has no route out except the egress proxy. It answers JSON only, with no rate limiter or image proxy, since its
only caller is the vessel.

## Failures are errors, not empty results

A search that cannot be completed reaches the model as an error, so the model never concludes that nothing exists:

| Situation | The model gets |
|---|---|
| Search service unreachable or returning an error status | An error naming the problem |
| SearXNG returns no results while some engines failed | An error saying engines failed or refused the query and that this does not mean nothing exists |
| DuckDuckGo returns its bot challenge (HTTP 202) | An error saying the engine refused the query |
| Real results | Titles, links and snippets, fenced as untrusted web content |
| A genuine empty answer | `no results` |
