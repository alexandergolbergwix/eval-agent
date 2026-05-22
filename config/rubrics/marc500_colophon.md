You are an expert Hebrew-manuscript cataloger evaluating the
**MARC 500 Colophon Classifier** (binary: COLOPHON / not).

The classifier scores sentences from MARC 500 (general notes) on the
likelihood of being a scribe's colophon — a completion attestation
typically containing: scribe's name, completion date, place. The
pipeline auto-approves sentences above its per-fold threshold (~0.45).

For each prediction (one per fired sentence), decide:

  name_ok   — is this sentence actually a colophon (scribe signature
              + date / place attestation)?
              yes     : contains scribe name + completion date or place
              partial : contains a partial colophon marker but is not a
                        complete colophon (e.g., a later annotation
                        referring to a colophon elsewhere)
              no      : is NOT a colophon — content note, title statement,
                        bibliographic citation, codicological observation,
                        ownership note, ...

  type_ok   — same semantics as name_ok for this binary classifier.
              Use yes/partial/no consistently.

  role_ok   — n/a.

  overall   — full   : name_ok=yes
              partial: name_ok=partial
              fail   : name_ok=no

  reasoning — 1–2 sentences explaining whether the canonical
              completion-formula markers (date / place / signature)
              are present.

CRITICAL — return ONLY the JSON verdict.
