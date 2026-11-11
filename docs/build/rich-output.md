# Rich output

An agent can attach structured visuals (tables, metrics, charts, cards) to its answer with the `present` tool. The
components follow [AstralPrimitives](https://github.com/AstralDeep/AstralPrimitives), the component schema Astral's
clients render.

!!! info "AstralPrimitives"
    AstralPrimitives (the `astralprims` package) is by the AstralDeep project and licensed under Apache 2.0.
    AstralVessel uses it, when installed, to validate components.

## Turn it on

```toml
[output]
primitives = true
```

The agent gets a `present` tool (scope `tools:read`), and its system prompt asks it to call `present` once before
its final message and still write the complete answer in Markdown. Components add to the answer; they never
replace it.

## What the model sends

`present` takes one argument, `components`, a list in display order:

```json
{"components": [
  {"type": "metric", "title": "Species", "value": "4"},
  {"type": "table", "title": "Lengths", "headers": ["Name", "cm"], "rows": [["Great Blue Heron", 115]]},
  {"type": "bar_chart", "title": "Length by species", "labels": ["Heron", "Hawk"],
   "datasets": [{"label": "cm", "data": [115, 56]}]}
]}
```

The tool description lists these shapes for the model: `metric`, `stat_group`, `table`, `bar_chart`, `line_chart`,
`key_value`, `list`, `card`, `alert` and `text`.

At most 12 components and 200,000 bytes of JSON per answer (see [Limits](../reference/limits.md#files-and-components)).
A later `present` call in the same turn replaces the earlier components.

## Validation

| `astralprims` installed | Check |
|---|---|
| Yes | Each component is validated and normalized by AstralPrimitives. The AstralVessel image installs `astralprims==0.12.0`. |
| No | Each component must be an object whose `type` is one of `metric`, `stat_group`, `table`, `bar_chart`, `line_chart`, `pie_chart`, `key_value`, `list`, `card`, `alert`, `text`, `badge`, `divider`, `container`, `grid`, `timeline`. |

An invalid list is refused with the first problem, and the model is asked to fix it and call `present` again.

To validate outside the image, for example in tests, install the optional extra:

```bash
pip install "astralvessel[primitives]"     # astralprims>=0.12,<1; from a checkout: pip install -e ".[primitives]"
```

## Where components appear

The `answer` artifact carries the Markdown text part and, when components were attached, a second part:

```json
{
  "mediaType": "application/vnd.astral.primitives+json",
  "data": {"_ui_components": [{"type": "metric", "title": "Species", "value": "4"}]},
  "metadata": {"schema_version": "0.12.0"}
}
```

`schema_version` is the installed `astralprims` version, or `unvalidated` without it. Astral renders components
from `_ui_components`. The agent card adds `application/vnd.astral.primitives+json` to its `defaultOutputModes`,
and the [Python client](../reference/python.md) returns them as the `answer` event's `components`.
