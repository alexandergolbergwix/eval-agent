You are evaluating a **Provenance NER** prediction (Hebrew manuscript
OWNER / DATE / COLLECTION extractor).

Source fields: `provenance` (MARC 561) primarily; also `notes[]` and
`colophon_text` for cross-referenced ownership info.

## Decision procedure

### Step 1 — Locate the span in MARC

Search `provenance`, `notes[]`, and `colophon_text` for the predicted
span. Apply the Universal definitions (exact, trimmed/extended,
vowelization variant).

Set `name_ok`:

- **yes**     — exact or vowelization variant match in any of those fields
- **partial** — trimmed/extended match
- **no**      — not present in any of those fields

### Step 2 — Verify `type_ok` per the predicted label

| Predicted | "yes" requires                                                                |
|-----------|-------------------------------------------------------------------------------|
| OWNER     | Surrounding text marks ownership: "בעלים", "שייך ל", "מספריית", "מאוסף <name>" pattern, OR provenance field explicitly lists the person as former owner |
| DATE      | Span is a date expression — Hebrew year ("ת"ה", "שנת...", "ה'תק..."), CE year ("1645"), or acquisition date phrase |
| COLLECTION| Span names an institution / library / archive — "ספריית X", "אוסף X", "Library", "ms. <city>" |

Set `type_ok`:

- **yes**     — surrounding text clearly marks the span as that type
- **partial** — right family, wrong specific label (e.g., predicted
                OWNER on a name that is the SCRIBE per `colophon_text`;
                OR predicted COLLECTION on a publisher name)
- **no**      — type clearly wrong (e.g., DATE on a person name, OWNER
                on a year)

### Step 3 — `role_ok = "n/a"`.

### Step 4 — Compute `overall` using the universal table.

## Worked examples

**Example A — full (OWNER):**
- Predicted: text="משה יהודה הכמ"ר מהללאל", type=OWNER
- MARC: `provenance = 'ציון בעלים בראש כה"י: "משה יהודה הכמ"ר מהללאל".'`
- Verdict: name_ok=yes (exact), type_ok=yes ("ציון בעלים" → ownership), overall=full
- Reasoning: `provenance: "ציון בעלים בראש כה"י: 'משה יהודה הכמ"ר מהללאל'" — explicitly marked as owner`

**Example B — full (DATE):**
- Predicted: text="ת"ה", type=DATE
- MARC: `colophon_text` contains "שנת ת"ה ליצירה"
- Verdict: name_ok=yes (exact), type_ok=yes (Hebrew year pattern), overall=full
- Reasoning: `colophon_text contains "שנת ת"ה ליצירה" — Hebrew year expression`

**Example C — fail (type wrong):**
- Predicted: text="יוסף בן יעקב", type=OWNER
- MARC: appears only in `colophon_text` as the scribe, not in `provenance`
- Verdict: name_ok=yes, type_ok=no (this person is the scribe, not the owner), overall=fail
- Reasoning: `name appears in colophon_text as scribe ("אני יוסף בן יעקב מעתיק"); not present in provenance — type OWNER is wrong`

## Output

JSON only. Cite the field name and quote the exact substring used.
