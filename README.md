# AstralVessel

A lightweight, open agent harness and the standard behind it. An AstralVessel agent is a folder
(manifest, briefing, declared tools) served over A2A, using MCP tools and any OpenAI-compatible
model, inside a locked-down sandbox with a single egress door. Its answers can carry
AstralPrimitives, so they render in Astral's clients, and the same package can later run inside
AstralDeep.

Status: **proposal**. Nothing is implemented yet.

- [PROPOSAL.md](PROPOSAL.md): what we would build, why, and the plan
- [docs/first-principles.md](docs/first-principles.md): every concept explained from scratch

Licensed under Apache 2.0, like the rest of Astral (license file to be added with the first code).
