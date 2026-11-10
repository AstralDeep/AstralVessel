# The present tool: lets an agent attach AstralPrimitives components to its answer.
# Components are validated against astralprims when it is installed, and structurally otherwise.

from __future__ import annotations

import json
from typing import Any

MAX_COMPONENTS = 12
MAX_BYTES = 200_000

PRESENT_DESCRIPTION = """Attach rich, structured visuals to your final answer (tables, metrics, charts, cards).
Call it once, after you have the facts, with the components to show. Your written answer must still
stand on its own in Markdown; components are an addition, never a replacement.
Supported component shapes (JSON):
- {"type":"metric","title":"Researchers","value":"35,740","subtitle":"optional"}
- {"type":"stat_group","title":"optional","items":[{"label":"Outputs","value":"76,080"}]}
- {"type":"table","title":"optional","headers":["Name","Outputs"],"rows":[["A. Smith",12],["B. Jones",9]]}
- {"type":"bar_chart","title":"By year","labels":["2022","2023"],"datasets":[{"label":"Outputs","data":[10,14]}]}
- {"type":"line_chart","title":"Trend","labels":["2022","2023"],"datasets":[{"label":"Rate","data":[1.5,1.9]}]}
- {"type":"key_value","title":"optional","items":[{"key":"ORCID","value":"0000-0002-..."}]}
- {"type":"list","items":["first","second"],"ordered":false}
- {"type":"card","title":"Heading","content":[ ...other components... ]}
- {"type":"alert","title":"optional","message":"A caveat the reader must see","variant":"warning"}
- {"type":"text","content":"A short paragraph."}"""

PRESENT_PARAMETERS = {
    "type": "object",
    "properties": {"components": {"type": "array", "items": {"type": "object"},
                                  "description": "AstralPrimitives components, in display order"}},
    "required": ["components"],
}

STRUCTURAL_TYPES = {"metric", "stat_group", "table", "bar_chart", "line_chart", "pie_chart", "key_value", "list",
                    "card", "alert", "text", "badge", "divider", "container", "grid", "timeline"}


def schema_version() -> str:
    try:
        import astralprims
    except ImportError:
        return "unvalidated"
    return str(astralprims.__version__)


def validate_components(components: Any) -> list[dict[str, Any]]:
    if not isinstance(components, list) or not components:
        raise ValueError("components must be a non-empty list")
    if len(components) > MAX_COMPONENTS:
        raise ValueError(f"at most {MAX_COMPONENTS} components are allowed")
    if len(json.dumps(components)) > MAX_BYTES:
        raise ValueError("components are too large")
    try:
        from astralprims import primitive_adapter
    except ImportError:
        for item in components:
            if not isinstance(item, dict) or item.get("type") not in STRUCTURAL_TYPES:
                raise ValueError("each component needs a supported type") from None
        return components
    cleaned = []
    for index, item in enumerate(components):
        try:
            cleaned.append(primitive_adapter.validate_python(item).to_dict())
        except Exception as exc:
            first = getattr(exc, "errors", lambda: [{}])()[0] if hasattr(exc, "errors") else {}
            where = ".".join(str(p) for p in first.get("loc", ())) or "component"
            raise ValueError(f"component {index} is invalid at {where}: {first.get('msg', 'bad shape')}") from None
    return cleaned
