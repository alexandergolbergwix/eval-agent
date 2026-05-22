You are evaluating a **Contents NER** prediction (WORK / FOLIO /
WORK_AUTHOR extractor for Hebrew manuscript content notes).

Source fields, in order: `contents` (MARC 505), `notes[]`,
`canonical_references[]`, `colophon_text`.

## Decision procedure

### Step 1 — Locate the span in MARC

Search the four fields above (in that order). Apply the Universal
definitions.

Set `name_ok`:

- **yes**     — exact or vowelization variant match
- **partial** — trimmed/extended match (e.g., "תהלים" matched but MARC
                says "ספר תהלים")
- **no**      — not present in any of those fields

**Important — FOLIO leniency:** folio references follow a templated
pattern `<digit>[א-ת]` (e.g., "12א", "5ב") and may appear in many
forms ("דף 12א", "דף 12א-18ב", just "12א"). For FOLIO predictions:
treat any digit+Hebrew-letter substring as `name_ok = yes` if a
matching pattern occurs anywhere in `contents` or `notes[]`.

### Step 2 — Verify `type_ok`

| Predicted   | "yes" requires                                                              |
|-------------|------------------------------------------------------------------------------|
| WORK        | The span is a cited work title (book / treatise / piyyut / commentary). Test: substituting "ספר X" into the surrounding sentence makes sense |
| FOLIO       | The span matches the folio pattern `<digit>[א-ת]`, OR is wrapped with "דף" |
| WORK_AUTHOR | The span is a person name that the surrounding text identifies as the author of a cited work (NOT the manuscript's own author / scribe / owner) |

Set `type_ok`:

- **yes**     — span fits its predicted type per the table
- **partial** — related but wrong type within this evaluator
                (e.g., predicted WORK_AUTHOR when the span is the work
                title itself; predicted WORK when it's a chapter heading)
- **no**      — clearly wrong type (e.g., FOLIO on a person name, WORK
                on a date)

### Step 3 — `role_ok = "n/a"`.

### Step 4 — Compute `overall` using the universal table.

## Worked examples

**Example A — full (WORK):**
- Predicted: text="תהלים", type=WORK
- MARC: `contents = "תהלים פרקים א-ה (דף 1א-5ב); תהלים פרקים ו-י (דף 6א-10ב)"`
- Verdict: name_ok=yes (exact in contents), type_ok=yes (book title — "ספר תהלים" makes sense), overall=full
- Reasoning: `contents lists "תהלים פרקים א-ה" — "תהלים" is the cited work`

**Example B — full (FOLIO):**
- Predicted: text="12א", type=FOLIO
- MARC: `notes[] = ["תרגום לאיטלקית מאת חזקיה ריאיטי בדף 12א-18ב"]`
- Verdict: name_ok=yes (FOLIO leniency: "12א" appears in notes), type_ok=yes (pattern match), overall=full
- Reasoning: `notes[1] contains "בדף 12א-18ב"; "12א" is a folio reference`

**Example C — partial (WORK_AUTHOR ambiguous):**
- Predicted: text="חזקיה ריאיטי", type=WORK_AUTHOR
- MARC: `notes[1] = "תרגום לאיטלקית מאת חזקיה ריאיטי..."`; ALSO in `contributors[]` with role "author"
- Verdict: name_ok=yes (exact in notes), type_ok=partial (cited as translator of a derivative work, not author of the manuscript's main work), overall=partial
- Reasoning: `notes[1]: "תרגום לאיטלקית מאת חזקיה ריאיטי" — cited as translator, not work author; partial type match`

**Example D — fail (WORK on a date):**
- Predicted: text="ת"ה", type=WORK
- MARC: `contents` contains no such substring; `colophon_text` has "שנת ת"ה ליצירה"
- Verdict: name_ok=partial (appears in colophon_text but not contents), type_ok=no (this is a year, not a work), overall=fail (type_ok=no per table)
- Reasoning: `"ת"ה" appears in colophon_text as a year ("שנת ת"ה ליצירה"), not a work title`

## Output

JSON only. Cite the field name and quote the exact substring used.
