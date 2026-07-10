# Firm deterministic auto-merge notes

## Change

- Updated `src/firm/resolve.py` so exact normalized-name pairs auto-accept deterministically with no LLM/web verification.
- Made normalization jurisdiction-aware: US legal suffix noise is stripped, while protected foreign legal forms stay identity-bearing tokens.
- Added deterministic rejection for explicit protected-foreign-vs-US legal-form conflicts, and for different protected foreign legal forms.
- Added F1 to the golden calibration summary.

No `--apply` or `--verify-llm` command was run.

## Dry-run counts

Command: `.venv/bin/python src/firm/resolve.py --mode full`

| Run | Candidate pairs | Accepted pairs | needs_review / review pairs | Rejected pairs | Clusters |
| --- | ---: | ---: | ---: | ---: | ---: |
| Before | 6,973 | 272 | 844 | 5,857 | 1,434 |
| After | 6,848 | 259 | 773 | 5,816 | 1,442 |

The review count fell by 71. Accepted pairs also fell by 13 because pairs such as US `Inc` vs foreign `Ltd` are no longer treated as safe alias merges.

## Golden calibration

Command: `.venv/bin/python src/firm/resolve.py --calibrate-golden evals/firm_resolution/golden_v1.json`

- Golden pairs: 8
- Accepted / review / rejected: 3 / 0 / 5
- TP=3 FP=0 FN=0 TN=3 uncertain=2
- Precision=1.000 recall=1.000 F1=1.000
- Thresholds unchanged.

## Foreign-suffix spot-check

Read-only accepted-pair audit:

- Accepted pairs: 259
- Review pairs: 773
- Rejected pairs: 5,816
- Accepted foreign-US legal-form conflicts: 0
- Accepted pairs with a foreign legal form: 4, all exact normalized-name variants of the same protected foreign form:
  - `Caraco Pharmaceutical Laboratories, Ltd.` <-> `Caraco Pharmaceutical Laboratories Ltd.`
  - `Hetero Labs Limited Unit V` <-> `Hetero Labs Limited (Unit V)`
  - `Allergan, PLC.` <-> `Allergan PLC`
  - `Aurobindo Pharma Ltd.` <-> `Aurobindo Pharma LTD`

## Human review / decisions still required

- `docs/adr/0005*` is not present in this checkout. I followed the issue prompt, `CONTEXT.md`, and ADR-0004. If ADR-0005 exists elsewhere and disagrees, re-review before applying this resolver.
- Remaining `needs_review` pairs are not adjudicated here. Foreign-vs-bare cases are intentionally left for review unless names normalize exactly.
- Parent Group edges and brand resolution are out of scope. No parent-group edge was created or proposed as accepted fact.
- `NOTES-firm.md` is included because the done-when checklist explicitly requires it, even though the ownership paragraph otherwise limits changed files to `src/firm/*` plus optional eval cases.
- `PROJECT_INDEX.md` was regenerated because the repository pre-commit check requires it after resolver symbol changes and the new notes file.
