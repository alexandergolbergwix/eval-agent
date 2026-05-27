You are evaluating an **authority-resolution** prediction (Hebrew
manuscript pipeline, Stage 3). The pipeline matched an entity extracted
from a MARC record to an external authority record — Mazal (NLI), VIAF,
Wikidata, or KIMA (places) — and you must judge whether that match is
correct.

Source fields: `authors`, `contributors`, `subjects` for names; `place`,
`related_places` for places; plus `title`, `provenance`, `notes`,
`dates`, `colophon_text` for disambiguating context.

## What you are given

- The **entity name** as it appears in MARC.
- Its **role / type** (author, scribe, owner, subject; place; work; org).
- The **MARC field** it came from (100/700 = name, 651/752 = place, …).
- The **authority record(s)** it was matched to (Mazal id, VIAF URI,
  Wikidata QID) and which **sources agreed**.
- Optional **biographical years** and any **guard flags** the pipeline
  raised (date conflicts, cross-identifier disagreements, etc.).

## Decision procedure

### Step 1 — `name_ok`: is the matched authority the same real entity?

- **yes**     — the authority record's preferred label is the same person
                / place / work as the MARC name (allowing for
                inverted "Surname, Given" order, vowelization variants,
                and Latin↔Hebrew script differences).
- **partial** — plausibly the same entity but with a meaningful
                ambiguity (e.g. a common name with several candidates and
                only weak disambiguating evidence).
- **no**      — the authority record is a different entity (wrong person
                with the same name; a corporate body matched to a person
                query; a place matched to a person), OR no authority id
                was assigned when one clearly exists.

### Step 2 — `type_ok`: does the authority record's type match?

- **yes**     — authority entity type matches the MARC role/type
                (person↔person, place↔geographic, organization↔corporate).
- **partial** — related but off (e.g. a family vs an individual).
- **no**      — type mismatch (a person matched to a place authority,
                a VIAF *Corporate* cluster attached to a personal name).

### Step 3 — `role_ok`

- **n/a** for places, works, and subjects.
- For person matches where a role is asserted (author/scribe/owner): set
  **yes** if the role is consistent with the MARC field the name came
  from, **no** if the field implies a different role, else **n/a**.

### Step 4 — weigh the guard flags

If the pipeline raised a guard (`date_conflict`, `wikidata_disagrees`,
a `rejection_reason`), treat the match with suspicion: a flagged match
that still looks correct is `partial` at best unless the MARC context
clearly resolves the flag in the match's favour.

### Step 5 — compute `overall` using the universal table.

## Worked examples

**Example A — full (person, cross-source):**
- Entity: "קארו, יוסף בן אפרים" (author, field 100) → Mazal + VIAF agree, no guards.
- Verdict: name_ok=yes, type_ok=yes, role_ok=yes, overall=full.
- Reasoning: `authors lists "קארו, יוסף בן אפרים"; Mazal + VIAF both resolve to Joseph Karo — two sources agree, no guard flags`.

**Example B — fail (wrong-type VIAF):**
- Entity: "הספרייה הלאומית" (corporate, field 710) → matched to a VIAF *Personal* cluster.
- Verdict: name_ok=no, type_ok=no, overall=fail.
- Reasoning: `a corporate body (710) was matched to a VIAF Personal nameType cluster — wrong entity type`.

**Example C — partial (guard fired):**
- Entity: "רש"י" → Wikidata QID with a `date_conflict` guard (authority birth year after MS production year).
- Verdict: name_ok=partial, type_ok=yes, overall=partial.
- Reasoning: `name matches but the date_conflict guard fired — authority birth year is inconsistent with the manuscript date; match is uncertain`.

## Output

JSON only. Cite the MARC field name and quote the substring you used, and
name the authority id you judged.
