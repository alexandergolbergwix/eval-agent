"""Authority-file existence checks for the agentic judge (VIAF + Wikidata).

The only outbound network capability beyond the Gemini API. Given a
name, returns the authority records that match so the judge can verify a
NER-extracted entity is a real, known person/place/work.

NEVER raises out of ``lookup`` — any per-source failure (network, parse,
HTTP) is swallowed and that source simply contributes no hits. The
agentic loop depends on this: an authority outage must not crash a run.

Honours ``EVAL_AGENT_NO_NETWORK`` (truthy) for hermetic / offline runs.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from eval_agent.client.rate_limiter import RateLimiter
from eval_agent.logging_setup import get_logger, truncate

log = get_logger("eval_agent.authority")

_VIAF_SRU = "https://viaf.org/viaf/search"
_WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# VIAF nameType per requested entity kind. The SRU response tags each
# cluster with ns2:nameType; we keep only clusters whose type matches so a
# corporate/geographic cluster never surfaces for a person query.
_KIND_TO_VIAF_INDEX = {
    "person": "local.personalNames",
    "place": "local.geographicNames",
    "work": "local.uniformTitleWorks",
}
_KIND_TO_VIAF_NAMETYPE = {
    "person": "Personal",
    "place": "Geographic",
    "work": "UniformTitleWork",
}


def _no_network() -> bool:
    return str(os.environ.get("EVAL_AGENT_NO_NETWORK", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass
class AuthorityHit:
    """One authority-file match."""

    source: str               # "viaf" | "wikidata"
    id: str                   # VIAF cluster id, or Wikidata QID
    label: str                # matched label as the source returns it
    extra: dict[str, Any] = field(default_factory=dict)


class AuthorityClient:
    """Existence + identifier lookup against VIAF and Wikidata."""

    def __init__(
        self,
        *,
        rpm: int = 60,
        enable_viaf: bool = True,
        enable_wikidata: bool = True,
        timeout: int = 20,
    ) -> None:
        self._rate = RateLimiter(rpm)
        self._enable_viaf = enable_viaf
        self._enable_wikidata = enable_wikidata
        self._timeout = timeout

    def lookup(self, name: str, kind: str = "person") -> list[AuthorityHit]:
        """Return authority hits for *name*. Never raises; [] on no match."""
        cleaned = (name or "").strip()
        if not cleaned or _no_network():
            return []
        kind = kind if kind in _KIND_TO_VIAF_INDEX else "person"
        hits: list[AuthorityHit] = []
        if self._enable_viaf:
            hits.extend(self._viaf(cleaned, kind))
        if self._enable_wikidata:
            hits.extend(self._wikidata(cleaned, kind))
        return hits

    # ── Sources ────────────────────────────────────────────────────────

    def _viaf(self, name: str, kind: str) -> list[AuthorityHit]:
        index = _KIND_TO_VIAF_INDEX[kind]
        want_type = _KIND_TO_VIAF_NAMETYPE[kind]
        query = f'{index} all "{name}"'
        params = {
            "query": query,
            "httpAccept": "application/json",
            "maximumRecords": "5",
        }
        url = f"{_VIAF_SRU}?{urllib.parse.urlencode(params)}"
        try:
            data = self._get_json(url, accept="application/json")
        except Exception as exc:  # noqa: BLE001
            log.debug("viaf.fail name=%s err=%s", name, truncate(str(exc), 200))
            return []
        out: list[AuthorityHit] = []
        records = (((data or {}).get("searchRetrieveResponse") or {}).get("records")) or {}
        record_list = records.get("record") if isinstance(records, dict) else None
        if isinstance(record_list, dict):
            record_list = [record_list]
        if not isinstance(record_list, list):
            return []
        for rec in record_list:
            if not isinstance(rec, dict):
                continue
            cluster = (((rec.get("recordData") or {}).get("ns2:VIAFCluster")) or {})
            if not isinstance(cluster, dict):
                continue
            name_type = str(cluster.get("ns2:nameType") or cluster.get("nameType") or "")
            if name_type and want_type and name_type != want_type:
                continue  # filter out non-matching cluster types
            viaf_id = str(cluster.get("ns2:viafID") or cluster.get("viafID") or "").strip()
            if not viaf_id:
                continue
            label = _viaf_main_heading(cluster) or name
            out.append(AuthorityHit(
                source="viaf", id=viaf_id, label=label,
                extra={"name_type": name_type or "?"},
            ))
        return out

    def _wikidata(self, name: str, kind: str) -> list[AuthorityHit]:
        # wbsearchentities can't filter by entity type; we return hits with
        # their descriptions so the judge can assess relevance. Hebrew first,
        # English fallback.
        for lang in ("he", "en"):
            params = {
                "action": "wbsearchentities",
                "search": name,
                "language": lang,
                "format": "json",
                "limit": "5",
            }
            url = f"{_WIKIDATA_API}?{urllib.parse.urlencode(params)}"
            try:
                data = self._get_json(url, accept="application/json")
            except Exception as exc:  # noqa: BLE001
                log.debug("wikidata.fail name=%s lang=%s err=%s",
                          name, lang, truncate(str(exc), 200))
                continue
            results = (data or {}).get("search") or []
            if not results:
                continue
            out: list[AuthorityHit] = []
            for r in results:
                if not isinstance(r, dict):
                    continue
                qid = str(r.get("id") or "").strip()
                if not qid:
                    continue
                out.append(AuthorityHit(
                    source="wikidata", id=qid,
                    label=str(r.get("label") or name),
                    extra={"description": str(r.get("description") or "")},
                ))
            if out:
                return out
        return []

    # ── HTTP ───────────────────────────────────────────────────────────

    def _get_json(self, url: str, *, accept: str) -> dict[str, Any]:
        self._rate.acquire()
        req = urllib.request.Request(
            url,
            headers={"Accept": accept, "User-Agent": "MHM-eval-agent/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


def _viaf_main_heading(cluster: dict[str, Any]) -> str:
    """Best-effort extraction of a display heading from a VIAF cluster."""
    headings = cluster.get("ns2:mainHeadings") or cluster.get("mainHeadings")
    if isinstance(headings, dict):
        data = headings.get("ns2:data") or headings.get("data")
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            text = data.get("ns2:text") or data.get("text")
            if isinstance(text, str):
                return text
    return ""


__all__ = ["AuthorityClient", "AuthorityHit"]
