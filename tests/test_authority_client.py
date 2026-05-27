"""Tests for AuthorityClient (mocked HTTP, no network)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from eval_agent.client.authority_client import AuthorityClient


class _Resp(io.BytesIO):
    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *a: Any) -> None:
        self.close()


def _json_resp(obj: dict[str, Any]) -> _Resp:
    return _Resp(json.dumps(obj).encode("utf-8"))


def test_viaf_personal_hit_filters_corporate(monkeypatch: pytest.MonkeyPatch) -> None:
    viaf = {"searchRetrieveResponse": {"records": {"record": [
        {"recordData": {"ns2:VIAFCluster": {
            "ns2:viafID": "111", "ns2:nameType": "Personal",
            "ns2:mainHeadings": {"ns2:data": {"ns2:text": "Karo, Joseph"}},
        }}},
        {"recordData": {"ns2:VIAFCluster": {
            "ns2:viafID": "222", "ns2:nameType": "Corporate",
        }}},
    ]}}}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        return _json_resp(viaf)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = AuthorityClient(enable_wikidata=False)
    hits = client.lookup("Karo", "person")
    ids = {h.id for h in hits}
    assert "111" in ids        # personal kept
    assert "222" not in ids    # corporate filtered out


def test_wikidata_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    wd = {"search": [
        {"id": "Q179101", "label": "Joseph Karo", "description": "rabbi"},
    ]}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        return _json_resp(wd)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = AuthorityClient(enable_viaf=False)
    hits = client.lookup("Joseph Karo", "person")
    assert hits and hits[0].source == "wikidata"
    assert hits[0].id == "Q179101"
    assert hits[0].extra.get("description") == "rabbi"


def test_source_error_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req, timeout=None):  # noqa: ANN001
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    client = AuthorityClient()
    hits = client.lookup("anyone", "person")   # must not raise
    assert hits == []


def test_no_network_env_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def spy(req, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        return _json_resp({})

    monkeypatch.setattr("urllib.request.urlopen", spy)
    monkeypatch.setenv("EVAL_AGENT_NO_NETWORK", "1")
    client = AuthorityClient()
    assert client.lookup("Karo", "person") == []
    assert calls["n"] == 0


def test_empty_name_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def spy(req, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        return _json_resp({})

    monkeypatch.setattr("urllib.request.urlopen", spy)
    client = AuthorityClient()
    assert client.lookup("   ", "person") == []
    assert calls["n"] == 0


def test_rate_limiter_acquired(monkeypatch: pytest.MonkeyPatch) -> None:
    acquired = {"n": 0}
    client = AuthorityClient(enable_wikidata=False)

    orig_acquire = client._rate.acquire

    def counting_acquire() -> None:
        acquired["n"] += 1
        orig_acquire()

    monkeypatch.setattr(client._rate, "acquire", counting_acquire)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _json_resp({"searchRetrieveResponse": {"records": {}}}),
    )
    client.lookup("Karo", "person")
    assert acquired["n"] >= 1
