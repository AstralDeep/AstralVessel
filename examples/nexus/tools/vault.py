# Declared tools for the nexus example: find vault pages by name and summarize one page's frontmatter.
# Reads JSON arguments on stdin and prints JSON, so the harness shows the model typed inputs and outputs.

import json
import re
import sys
from pathlib import Path

WIKI = Path.cwd() / "wiki"
TYPES = ("person", "topic", "method", "organization", "grant", "publication", "facility", "initiative")
LIST_FIELDS = ("related",)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def frontmatter(path):
    meta, lines = {}, path.read_text(encoding="utf-8", errors="replace").split("\n")
    if not lines or lines[0] != "---":
        return meta
    for line in lines[1:]:
        if line == "---":
            break
        key, _, value = line.partition(":")
        value = value.strip()
        try:
            meta[key.strip()] = json.loads(value) if value[:1] in '["{' or value.isdigit() else value
        except ValueError:
            meta[key.strip()] = value
    return meta


def find_pages(args):
    tokens = [t for t in slugify(args.get("query", "")).split("-") if t]
    kind = args.get("type")
    if not tokens:
        return {"error": "query is empty"}
    prefixes = [f"{kind}-"] if kind in TYPES else [f"{t}-" for t in TYPES]
    hits = []
    for name in sorted(p.name for p in WIKI.iterdir()):
        if not name.endswith(".md") or not name.startswith(tuple(prefixes)):
            continue
        slug = name[:-3]
        words = slug.split("-")[1:]
        if all(any(w.startswith(t) for w in words) for t in tokens):
            hits.append(slug)
    limit = min(int(args.get("limit") or 25), 100)
    out = []
    for slug in hits[:limit]:
        meta = frontmatter(WIKI / f"{slug}.md")
        out.append({"slug": slug, "title": meta.get("title", slug),
                    "publication_count": meta.get("publication_count")})
    return {"matches": out, "total": len(hits)}


def profile(args):
    slug = slugify(args.get("slug", ""))
    path = WIKI / f"{slug}.md"
    if not path.is_file():
        return {"error": f"no page {slug}; use find_pages first"}
    meta = frontmatter(path)
    related = meta.get("related") or []
    keep = {k: v for k, v in meta.items()
            if k not in LIST_FIELDS and k not in ("sources", "aliases", "tags", "conflicts", "created", "updated",
                                                   "status", "identity_bases", "merged_identity_keys")
            and v not in ("", [], {}, None)}
    groups = {}
    for item in related:
        groups.setdefault(item.split("-", 1)[0], []).append(item)
    keep["related"] = {k: v[:25] for k, v in groups.items()}
    keep["related_totals"] = {k: len(v) for k, v in groups.items()}
    return keep


if __name__ == "__main__":
    command = sys.argv[1]
    arguments = json.loads(sys.stdin.read() or "{}")
    result = {"find_pages": find_pages, "profile": profile}[command](arguments)
    print(json.dumps(result, ensure_ascii=False, indent=1))
