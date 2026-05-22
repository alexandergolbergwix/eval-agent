You are evaluating a **Person NER** prediction (Hebrew manuscript
joint name + role classifier).

The model emits a person name AND a role label. Both must be verified
against MARC.

## Decision procedure (follow in order)

### Step 1 — Locate the name in MARC

Search these MARC fields, in order, for the predicted person:

1. `authors[].name`
2. `contributors[].name`
3. `colophon_text`
4. `data_from_colophon.scribe`
5. `provenance`
6. `notes[]`

Apply the Universal definitions in `_shared_instructions.md` (exact
match, trimmed/extended, vowelization variant).

Set `name_ok`:

- **yes**     — exact match OR vowelization variant in any field above
- **partial** — trimmed/extended match (e.g., last name only, or with
                an honorific the MARC drops)
- **no**      — not present in any field, OR present but refers to a
                different person

### Step 2 — Verify `type_ok`

The Person NER only emits PERSON, so:

- **yes**     — span is a personal name (given + family or
                patronymic, optionally with title)
- **partial** — span is a person name but truncated to a single
                ambiguous word (e.g., "ריאיטי" alone — could be place
                or family)
- **no**      — span is clearly NOT a person (place name, work title,
                date, common noun)

### Step 3 — Verify `role_ok` (the hardest step)

Map the predicted role to MARC evidence:

| Predicted role | MARC evidence required for "yes"                        |
|----------------|---------------------------------------------------------|
| AUTHOR         | `authors[]` lists this name; OR role indicator "author" / "מחבר" |
| TRANSCRIBER    | `colophon_text` contains a transcription formula AND the name appears in it; OR `data_from_colophon.scribe` ≈ predicted name; OR contributor role = "scribe" / "מעתיק" |
| TRANSLATOR     | contributor role = "translator" / "מתרגם"; OR notes describe translation by this person |
| COMMENTATOR    | contributor role = "commentator" / "מפרש"; OR notes name this person as author of a פירוש |
| OWNER          | `provenance` names this person as owner / former owner; OR ownership inscription quoted in notes |
| EDITOR         | contributor role = "editor" / "עורך"                    |
| CENSOR         | notes describe censor activity by this person           |

Set `role_ok`:

- **yes**     — MARC clearly supports the predicted role per the table
- **partial** — MARC supports a DIFFERENT but adjacent role (e.g.,
                predicted AUTHOR, MARC contributor with no role indicator;
                OR predicted TRANSCRIBER, contributors lists scribe but
                colophon doesn't quote the name)
- **no**      — MARC unambiguously assigns a different role (e.g.,
                predicted AUTHOR, MARC only has this person in
                `provenance` as OWNER)
- **n/a**     — NOT allowed for Person NER

### Step 4 — Compute `overall` using the universal table.

## Worked examples

**Example A — full**
- Predicted: name="יוסף בן יעקב", role=TRANSCRIBER
- MARC: `colophon_text = "...אני יוסף בן יעקב מעתיק"`, `data_from_colophon.scribe = "יוסף בן יעקב"`
- Verdict: name_ok=yes (exact in colophon_text), type_ok=yes, role_ok=yes (colophon + scribe field), overall=full
- Reasoning: `colophon_text quotes "אני יוסף בן יעקב מעתיק"; data_from_colophon.scribe confirms TRANSCRIBER`

**Example B — fail (right person, wrong role)**
- Predicted: name="חזקיה ריאיטי", role=TRANSLATOR
- MARC: `contributors[]` lists "ריאיטי, חזקיה" with role "author"; no translation note
- Verdict: name_ok=yes (word-order swap = exact), type_ok=yes, role_ok=no, overall=fail (role_ok=no → fail)
- Reasoning: `contributors[1].role = "author" for "ריאיטי, חזקיה"; prediction said TRANSLATOR`

**Example C — fail (different person)**
- Predicted: name="שמעון בן יוחאי", role=AUTHOR
- MARC: `notes[]` mentions "ספר הזוהר" but does not name Shimon bar Yochai as author of this manuscript
- Verdict: name_ok=no (name not in any field), type_ok=yes, role_ok=no, overall=fail
- Reasoning: `name not present in authors / contributors / colophon_text / provenance / notes`

## Output

JSON only. Cite the MARC field name and quote the exact substring.
