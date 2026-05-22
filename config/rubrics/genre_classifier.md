You are evaluating a **Genre Classifier** prediction (multi-label,
8 Hebrew-manuscript classes + NOTA "none of the above").

The classifier predicts MARC 655 genre/form headings from title +
general notes. Gold reference, when present, is the `genres` field in
MARC. About 31% of records have NO gold — the model exists for those.

## The 8 classes + NOTA

The classifier may fire on any of these labels:

`Piyyutim`, `Poetry`, `Illustrated works (Manuscript)`,
`Personal correspondence`, `Censored manuscripts`,
`Autograph manuscripts`, `Records (Documents)`, `Bibliographies`, and
`other` (NOTA).

## Decision procedure

### Step 1 — Check gold

If `genres` (MARC 655) is non-empty:

- **Predicted label is in `genres`**            → `name_ok = yes`, `type_ok = yes`
- **Predicted label is semantically adjacent** → `name_ok = partial`, `type_ok = partial`
  (e.g., predicted "Poetry" when gold says "Piyyutim"; predicted
  "Records (Documents)" when gold says "Letters")
- **Predicted label is unrelated to gold**     → `name_ok = no`, `type_ok = no`

### Step 2 — No gold? Triangulate from title + notes + subjects

Evidence patterns:

| Predicted label                  | Evidence pattern in title / notes / subjects                     |
|----------------------------------|------------------------------------------------------------------|
| Piyyutim                         | "פיוט", "פיוטים", liturgical-poetry keywords ("סליחות", "קינות") |
| Poetry                           | "שירה", "שירים", verse structure mentioned                       |
| Illustrated works (Manuscript)   | "מאויר", "ציורים", "illuminated", references to miniatures        |
| Personal correspondence          | "מכתבים", "אגרות", "letters"                                     |
| Censored manuscripts             | "צנזורה", "מצונזר", inquisitor / censor names in notes           |
| Autograph manuscripts            | "אוטוגרף", "כתב יד המחבר", "autograph"                            |
| Records (Documents)              | "תעודות", "documents", legal / archival content                 |
| Bibliographies                   | "ביבליוגרפיה", "רשימת ספרים", catalogues of works                |

- **Pattern clearly present**                → `name_ok = yes`, `type_ok = yes`
- **Weak signal, related but uncertain**     → `name_ok = partial`, `type_ok = partial`
- **No supporting evidence in title / notes / subjects** → `name_ok = no`, `type_ok = no`

### Step 3 — `role_ok = "n/a"`.

### Step 4 — Compute `overall` using the universal table.

## Worked examples

**Example A — full (gold match):**
- Predicted: "Piyyutim"
- MARC: `genres = ["Piyyutim", "Autograph manuscripts"]`
- Verdict: name_ok=yes, type_ok=yes, overall=full
- Reasoning: `genres[0] = "Piyyutim" — exact match`

**Example B — full (no gold, strong evidence):**
- Predicted: "Illustrated works (Manuscript)"
- MARC: `genres = []`; `notes[] = ["כתב יד מאויר עם מיניאטורות זהב"]`
- Verdict: name_ok=yes, type_ok=yes, overall=full
- Reasoning: `notes[0] contains "מאויר עם מיניאטורות" — illustrated-works pattern`

**Example C — partial (adjacent):**
- Predicted: "Poetry"
- MARC: `genres = ["Piyyutim"]`
- Verdict: name_ok=partial, type_ok=partial, overall=partial
- Reasoning: `Piyyutim are religious poetry; predicted Poetry is the secular sibling — adjacent`

**Example D — fail (unsupported):**
- Predicted: "Personal correspondence"
- MARC: `genres = ["Bible commentaries"]`; `notes[]` mentions Psalm commentary; no letters / correspondence
- Verdict: name_ok=no, type_ok=no, overall=fail
- Reasoning: `genres = "Bible commentaries"; notes describe biblical exegesis — no correspondence evidence`

## Output

JSON only. Cite the field used (`genres`, `notes[i]`, `subjects[i]`,
`title`) and quote the deciding substring.
