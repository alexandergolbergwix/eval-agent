"""Tests for the agentic tool layer + trace (no network, no Gemini)."""

from __future__ import annotations

from typing import Any

from eval_agent.agentic.tools import TOOL_DECLS, ToolContext, ToolRegistry
from eval_agent.agentic.trace import Trace


def _ctx() -> ToolContext:
    marc = {
        "990001": {
            "_control_number": "990001",
            "title": "ספר התשובות",
            "authors": ["קארו, יוסף בן אפרים"],
            "notes": ["source.mrc", "בעלים: שלמה בן יצחק", "נכתב בצפת"],
            "colophon_text": "נשלם הספר על ידי הסופר",
        }
    }
    ner = {
        "990001": {
            "_control_number": "990001",
            "entities": [
                {"text": "קארו, יוסף", "type": "PERSON", "role": "AUTHOR",
                 "source": "person_ner", "confidence": 0.85},
                {"text": "צפת", "type": "PLACE", "source": "provenance_ner",
                 "confidence": 0.7},
            ],
        }
    }
    return ToolContext(record_id="990001", marc_index=marc, ner_index=ner, max_chars=4000)


class TestFetchMarcField:
    def test_returns_field(self) -> None:
        reg = ToolRegistry(["fetch_marc_field"])
        out = reg.dispatch("fetch_marc_field", {"field": "title"}, _ctx())
        assert "ספר התשובות" in out

    def test_missing_field_lists_available(self) -> None:
        reg = ToolRegistry(["fetch_marc_field"])
        out = reg.dispatch("fetch_marc_field", {"field": "nonsuch"}, _ctx())
        assert "not present" in out
        assert "title" in out and "authors" in out


class TestExpandNote:
    def test_returns_full_notes_skipping_filename(self) -> None:
        reg = ToolRegistry(["expand_note"])
        out = reg.dispatch("expand_note", {}, _ctx())
        assert "בעלים: שלמה בן יצחק" in out
        assert "נכתב בצפת" in out
        assert "נשלם הספר" in out          # colophon included
        assert "source.mrc" not in out      # filename marker stripped


class TestListRecordEntities:
    def test_lists_all_sources(self) -> None:
        reg = ToolRegistry(["list_record_entities"])
        out = reg.dispatch("list_record_entities", {}, _ctx())
        assert "person_ner" in out and "provenance_ner" in out
        assert "קארו" in out and "צפת" in out


class _StubAuthority:
    def __init__(self, hits: list[Any]) -> None:
        self._hits = hits

    def lookup(self, name: str, kind: str = "person") -> list[Any]:
        return self._hits


class TestLookupAuthority:
    def test_unavailable_without_client(self) -> None:
        reg = ToolRegistry(["lookup_authority"])
        out = reg.dispatch("lookup_authority", {"name": "Karo"}, _ctx())
        assert "unavailable" in out

    def test_formats_hits(self) -> None:
        from eval_agent.client.authority_client import AuthorityHit
        ctx = _ctx()
        ctx.authority = _StubAuthority([
            AuthorityHit(source="viaf", id="12345", label="Karo, Joseph",
                         extra={"name_type": "Personal"}),
        ])
        reg = ToolRegistry(["lookup_authority"])
        out = reg.dispatch("lookup_authority", {"name": "Karo", "kind": "person"}, ctx)
        assert "viaf:12345" in out and "Karo, Joseph" in out

    def test_no_match(self) -> None:
        ctx = _ctx()
        ctx.authority = _StubAuthority([])
        reg = ToolRegistry(["lookup_authority"])
        out = reg.dispatch("lookup_authority", {"name": "Nobody"}, ctx)
        assert "no authority match" in out


class TestRegistry:
    def test_declarations_only_enabled(self) -> None:
        reg = ToolRegistry(["fetch_marc_field", "expand_note"])
        decls = reg.declarations()
        names = {d["name"] for d in decls[0]["functionDeclarations"]}
        assert names == {"fetch_marc_field", "expand_note"}

    def test_disabled_tool_returns_error_string(self) -> None:
        reg = ToolRegistry(["fetch_marc_field"])
        out = reg.dispatch("lookup_authority", {"name": "x"}, _ctx())
        assert "not enabled" in out

    def test_unknown_tool_no_raise(self) -> None:
        reg = ToolRegistry(["fetch_marc_field"])
        out = reg.dispatch("does_not_exist", {}, _ctx())
        assert "not enabled" in out or "unknown" in out

    def test_all_four_declared(self) -> None:
        names = {d["name"] for d in TOOL_DECLS}
        assert names == {
            "fetch_marc_field", "expand_note", "list_record_entities", "lookup_authority",
        }


class TestTruncation:
    def test_observation_truncated(self) -> None:
        marc = {"r": {"_control_number": "r", "title": "x" * 9000}}
        ctx = ToolContext(record_id="r", marc_index=marc, ner_index={}, max_chars=500)
        reg = ToolRegistry(["fetch_marc_field"])
        out = reg.dispatch("fetch_marc_field", {"field": "title"}, ctx)
        assert len(out) <= 501


class TestTrace:
    def test_roundtrip(self) -> None:
        t = Trace(record_id="990001", evaluator_id="person_ner", sub_type="AUTHOR")
        t.add(tool="fetch_marc_field", args={"field": "title"}, observation="title: x")
        t.add(tool=None, note="verdict")
        t.final_model = "gemini-3.5-flash"
        d = t.to_dict()
        assert d["record_id"] == "990001"
        assert d["tools_used"] == ["fetch_marc_field"]
        assert len(d["steps"]) == 2
        assert d["steps"][0]["tool"] == "fetch_marc_field"
