You are evaluating a **MARC 500 Colophon Classifier** prediction
(binary: COLOPHON / not). The classifier scores sentences from MARC
500 (general notes) for the likelihood of being a scribe's colophon.

A colophon, in this corpus, is a SCRIBE'S COMPLETION ATTESTATION at
the end of a manuscript. It typically contains AT LEAST TWO of three
markers below; commonly all three.

## The three colophon markers

| # | Marker             | Patterns                                                                              |
|---|--------------------|---------------------------------------------------------------------------------------|
| 1 | Scribe identity    | "אני <name>", "כתבתי", "כתב <name>", "סופר <name>", "מעתיק <name>"                    |
| 2 | Completion formula | "נשלם", "תם ונשלם", "סיימתי", "השלמתי", "כליל", "הסיום"                                |
| 3 | Date or place      | Year (Hebrew: "שנת ה'", "שנת ת...", "ליצירה"); place ("ב<city>", "פה <city>")          |

## Decision procedure

### Step 1 — Count markers

Scan the predicted sentence for the three markers above. Let `M` be
the number of distinct markers present (0, 1, 2, or 3).

### Step 2 — Apply rules

- **M ≥ 2** → `name_ok = yes`, `type_ok = yes`. A real colophon.
- **M = 1** → `name_ok = partial`, `type_ok = partial`. A partial /
              embedded colophon reference. Examples: a later
              annotation citing the colophon, OR a transcription
              statement with no completion or date.
- **M = 0** → `name_ok = no`, `type_ok = no`. Not a colophon.

### Common false positives (M = 0)

- Title statement: "ספר תהלים עם פירוש רש״י"
- Ownership inscription: "שייך ל<name>"
- Content note: "תרגום מאת <name>"
- Bibliographic citation: reference to a printed edition
- Codicological observation: size, hand, layout
- Provenance / acquisition note

### Step 3 — `role_ok = "n/a"` (binary classifier, no role).

### Step 4 — Compute `overall` using the universal table.

## Worked examples

**Example A — full (M = 3):**
- Sentence: `נשלם פירוש כל ספר תהלים בעזרת ה' יום ה' כ"ח לאדר שנת ת"ה ליצירה אני יוסף בן יעקב מעתיק`
- Markers: completion ("נשלם"), date ("שנת ת״ה ליצירה"), scribe ("אני יוסף בן יעקב מעתיק")
- Verdict: name_ok=yes, type_ok=yes, role_ok=n/a, overall=full
- Reasoning: `three markers present — completion "נשלם", date "שנת ת"ה ליצירה", scribe "אני יוסף בן יעקב מעתיק"`

**Example B — fail (M = 0, title statement):**
- Sentence: `בכתב יד זה ספר תהלים עם פירוש רש"י`
- Markers: none — no completion formula, no scribe, no date
- Verdict: name_ok=no, type_ok=no, role_ok=n/a, overall=fail
- Reasoning: `title statement, not a colophon — zero markers`

**Example C — partial (M = 1, partial colophon):**
- Sentence: `העתיק זאת אברהם הסופר`
- Markers: scribe identity only ("העתיק זאת אברהם"); no completion formula, no date
- Verdict: name_ok=partial, type_ok=partial, role_ok=n/a, overall=partial
- Reasoning: `scribe identity only ("העתיק זאת אברהם"); no completion or date — partial colophon`

**Example D — fail (provenance):**
- Sentence: `ציון בעלים: משה יהודה מהללאל`
- Markers: none (this is a provenance / ownership note)
- Verdict: name_ok=no, type_ok=no, role_ok=n/a, overall=fail
- Reasoning: `ownership inscription, not a colophon — names an owner, not a scribe`

## Output

JSON only. In `reasoning`, list the markers found by name, or state
explicitly which false-positive category the sentence belongs to if
zero markers are present.
