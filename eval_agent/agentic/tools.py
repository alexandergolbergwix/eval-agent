"""Tools the agentic judge can call to gather evidence on demand.

Each tool is ``(args, ctx) -> str`` returning an observation string shown
back to the model. Tools NEVER raise — bad input returns a short
diagnostic string so the loop keeps going.

Three tools read the pipeline's on-disk JSON (no network); ``lookup_authority``
calls out via the injected ``AuthorityClient`` (the only networked tool).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eval_agent.client.authority_client import AuthorityClient


@dataclass
class ToolContext:
    """Everything the tools need, scoped to one candidate's record."""

    record_id: str
    marc_index: dict[str, dict[str, Any]]   # control_number -> full marc record
    ner_index: dict[str, dict[str, Any]]    # control_number -> full ner record
    authority: "AuthorityClient | None" = None
    max_chars: int = 4000


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _marc_record(ctx: ToolContext) -> dict[str, Any] | None:
    return ctx.marc_index.get(ctx.record_id)


def _ner_record(ctx: ToolContext) -> dict[str, Any] | None:
    return ctx.ner_index.get(ctx.record_id)


# ── Tool implementations ────────────────────────────────────────────────


def _tool_fetch_marc_field(args: dict[str, Any], ctx: ToolContext) -> str:
    field = str(args.get("field") or "").strip()
    rec = _marc_record(ctx)
    if rec is None:
        return f"could not find MARC record for id {ctx.record_id}"
    if not field:
        return "missing required argument 'field'. available fields: " + ", ".join(sorted(rec.keys()))
    if field not in rec:
        return (
            f"field '{field}' not present. available fields: "
            + ", ".join(sorted(rec.keys()))
        )
    value = rec[field]
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return _truncate(f"{field}: {rendered}", ctx.max_chars)


def _tool_expand_note(args: dict[str, Any], ctx: ToolContext) -> str:
    rec = _marc_record(ctx)
    if rec is None:
        return f"could not find MARC record for id {ctx.record_id}"
    chunks: list[str] = []
    for key in ("notes", "colophon_text", "data_from_colophon"):
        v = rec.get(key)
        if not v:
            continue
        if isinstance(v, list):
            # notes[0] is the pipeline's source-filename marker; keep the rest.
            body = v[1:] if key == "notes" and len(v) > 1 else v
            rendered = " | ".join(
                json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else str(x)
                for x in body if x
            )
        elif isinstance(v, dict):
            rendered = json.dumps(v, ensure_ascii=False)
        else:
            rendered = str(v)
        if rendered.strip():
            chunks.append(f"{key}: {rendered}")
    if not chunks:
        return "no notes / colophon text on this record"
    return _truncate("\n".join(chunks), ctx.max_chars)


def _tool_list_record_entities(args: dict[str, Any], ctx: ToolContext) -> str:
    rec = _ner_record(ctx)
    if rec is None:
        return f"could not find NER record for id {ctx.record_id}"
    entities = rec.get("entities") or []
    if not entities:
        return "no NER predictions on this record"
    lines: list[str] = []
    for e in entities:
        if not isinstance(e, dict):
            continue
        text = e.get("person") or e.get("text") or e.get("value") or ""
        kind = e.get("type") or e.get("role") or ""
        source = e.get("source") or "?"
        conf = e.get("confidence")
        lines.append(f"- {text} | {kind} | {source} | conf={conf}")
    body = "\n".join(lines) if lines else "no usable entities"
    return _truncate(body, ctx.max_chars)


def _tool_lookup_authority(args: dict[str, Any], ctx: ToolContext) -> str:
    name = str(args.get("name") or "").strip()
    kind = str(args.get("kind") or "person").strip() or "person"
    if not name:
        return "missing required argument 'name'"
    if ctx.authority is None:
        return "authority lookup unavailable (no authority client configured)"
    try:
        hits = ctx.authority.lookup(name, kind)
    except Exception as exc:  # noqa: BLE001 — defensive; lookup should not raise
        return f"authority lookup failed: {exc}"
    if not hits:
        return f"no authority match found for '{name}' ({kind})"
    lines = []
    for h in hits:
        extra = ", ".join(f"{k}={v}" for k, v in (h.extra or {}).items() if v)
        suffix = f" ({extra})" if extra else ""
        lines.append(f"- {h.source}:{h.id} — {h.label}{suffix}")
    return _truncate("\n".join(lines), ctx.max_chars)


_TOOL_FNS = {
    "fetch_marc_field": _tool_fetch_marc_field,
    "expand_note": _tool_expand_note,
    "list_record_entities": _tool_list_record_entities,
    "lookup_authority": _tool_lookup_authority,
}


# Gemini functionDeclarations. Descriptions steer the model's tool choice —
# they emphasise "only when needed" to keep cheap candidates cheap.
TOOL_DECLS: list[dict[str, Any]] = [
    {
        "name": "fetch_marc_field",
        "description": (
            "Read one field verbatim from the manuscript's full MARC record. "
            "Use only when the context already shown is missing a field you "
            "need to judge the prediction (e.g. an author, place, or date field)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "description": "MARC semantic field name to read"},
            },
            "required": ["field"],
        },
    },
    {
        "name": "expand_note",
        "description": (
            "Return the full untruncated catalog notes + colophon text for this "
            "manuscript. Use when the shown context truncates a note you need to "
            "verify an owner, date, or attribution."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_record_entities",
        "description": (
            "List every NER prediction on this manuscript (people, places, works). "
            "Use to reason jointly — e.g. to check whether two predictions refer to "
            "the same entity, or whether a name appears elsewhere on the record."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "lookup_authority",
        "description": (
            "Check whether a name exists in VIAF / Wikidata authority files and "
            "return matching identifiers. Use to confirm a person/place/work is a "
            "real, known entity when the catalog context alone is inconclusive."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "the name to look up"},
                "kind": {
                    "type": "string",
                    "enum": ["person", "place", "work"],
                    "description": "entity kind (default person)",
                },
            },
            "required": ["name"],
        },
    },
]

_DECLS_BY_NAME = {d["name"]: d for d in TOOL_DECLS}


class ToolRegistry:
    """Holds the enabled subset of tools + dispatches calls."""

    def __init__(self, enabled: list[str]) -> None:
        self._enabled = [t for t in enabled if t in _TOOL_FNS]

    @property
    def enabled(self) -> list[str]:
        return list(self._enabled)

    def declarations(self) -> list[dict[str, Any]]:
        """Gemini ``tools`` array for the enabled tools only."""
        decls = [_DECLS_BY_NAME[name] for name in self._enabled if name in _DECLS_BY_NAME]
        return [{"functionDeclarations": decls}] if decls else []

    def dispatch(self, name: str, args: dict[str, Any], ctx: ToolContext) -> str:
        if name not in self._enabled:
            return f"tool '{name}' is not enabled (enabled: {', '.join(self._enabled) or 'none'})"
        fn = _TOOL_FNS.get(name)
        if fn is None:
            return f"unknown tool '{name}'"
        try:
            return fn(args or {}, ctx)
        except Exception as exc:  # noqa: BLE001 — tools must never raise into the loop
            return f"tool '{name}' errored: {exc}"


__all__ = ["ToolContext", "ToolRegistry", "TOOL_DECLS"]
