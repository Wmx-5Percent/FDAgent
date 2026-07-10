# Taxonomy induction notes — draft v1 dry run

Issue #20 dry-run deliverable. No `--apply` command was run; no `taxonomy` rows were frozen/seeded and no `recall_label` rows were written.

## Run summary

- Source: `drug_enforcement.reason_for_recall`
- Input size: 17,723 recall records with 4,390 distinct non-empty reason texts
- Induction command attempted: `src/classify/induce.py --output-file data/processed/taxonomy_draft_v1.json`
- LLM path result: failed once because no chat API key was configured (`ProviderMissingKeyError`)
- Fallback used: `src/classify/induce.py --no-llm --output-file data/processed/taxonomy_draft_v1.json`
- Draft artifact: `data/processed/taxonomy_draft_v1.json` (git-ignored)
- Label-distribution artifact: `data/processed/taxonomy_draft_v1_label_distribution.json` (git-ignored)
- Distribution method: `label.py --taxonomy-file data/processed/taxonomy_draft_v1.json --draft-prefix-match`; dry-run prefix matching only, no LLM/cache/DB writes
- ADR note: `docs/adr/0005` was not present in this checkout; issue #20's reversible-only rules were followed.

Counts below are approximate draft-distribution counts from prefix matching against the induced draft, not frozen taxonomy labels.

## Draft two-level node list and distribution

### quality_and_potency — Quality and potency (~9,216)

Recall reasons describing failed product quality attributes, potency, specifications, contamination, sterility, or stability.

| Node | Approx recalls | Sample reasons |
| --- | ---: | --- |
| `sterility_assurance` — Sterility assurance | 6,098 | Lack of Assurance of Sterility<br>Lack of sterility assurance.<br>Lack of Assurance of Sterility; FDA inspection identified GMP violations potentially impacting product quality and sterility |
| `microbial_contamination` — Microbial contamination | 725 | Microbial contamination<br>Microbial Contamination of Non-Sterile Products<br>Microbial Contamination of Non-Sterile Products: Product is being recalled due to possible microbial contamination by C. difficile discovered in the raw material. |
| `potency_or_content` — Potency or content | 685 | Subpotent Drug<br>Subpotent Drug:The titratable iodine contained in the Povidone-Iodine prep pads is below label claim of 0.85%.<br>Subpotent drug |
| `impurities_or_degradation` — Impurities or degradation | 546 | Failed Impurities/Degradation Specifications<br>Failed Impurities/Degradation Specifications; Out of specification result obtained for impurity A during stability testing.<br>Failed Impurities/Degradation Specifications. |
| `dissolution_or_tablet_specs` — Dissolution or tablet specifications | 461 | Failed Dissolution Specifications<br>Failed dissolution specifications<br>Failed Dissolution Specification |
| `particulate_or_foreign_matter` — Particulate or foreign matter | 430 | Presence of Particulate Matter: API contaminated with glass particulate was used to produce sterile injectable drugs.<br>Presence of Particulate matter: manufacturer recalled fentanyl API due to potential for glass particules<br>Presence of Particulate Matter |
| `stability_or_expiry` — Stability or expiry | 222 | Stability Data Does Not Support Expiry: potential loss of potency in drugs packaged and stored in syringes.<br>Failed Stability Specifications<br>Stability data does not support expiry. |
| `appearance_or_physical_defect` — Appearance or physical defect | 49 | Discoloration<br>Discoloration.<br>Discoloration: discolored tablets (shades of blue) mixed in with the white inert remainder tablets. |

### manufacturing_controls — Manufacturing controls (~4,201)

Recall reasons describing manufacturing practice, process-control, or cross-contamination control failures.

| Node | Approx recalls | Sample reasons |
| --- | ---: | --- |
| `cgmp_deviation` — cGMP deviation | 3,201 | CGMP Deviations<br>CGMP Deviations: Intermittent exposure to temperature excursion during storage.<br>cGMP deviations |
| `cross_contamination` — Cross contamination | 674 | Penicillin Cross Contamination: All lots of all products repackaged and distributed between 01/05/12 and 02/12/15 are being recalled because they were repackaged in a facility with penicillin products without adequate separation which could introduce the potential for cross contamination with penicillin.<br>Cross Contamination With Other Products: Oral care solutions were manufactured by a third party supplier on equipment shared with non-pharmaceutical products<br>Penicillin Cross Contamination |
| `processing_controls` — Processing controls | 326 | Lack of Processing Controls.<br>Lack of Processing Controls<br>Lack of Processing Control |

### labeling_and_packaging — Labeling and packaging (~1,601)

Recall reasons describing labeling, package, container, closure, or delivery-system defects.

| Node | Approx recalls | Sample reasons |
| --- | ---: | --- |
| `labeling_error` — Labeling error | 1,313 | Labeling: Not Elsewhere Classified: Products may contain synthetic latex and/or natural latex.<br>Labeling: Incorrect or Missing Lot and/or Exp Date: Beyond Use Date (BUD) exceed the BUD/EXP of at least one ingredient used to make final product.<br>Labeling: Incorrect or missing lot and/or expiration date. Products were compounded using expired components. |
| `container_or_closure_defect` — Container or closure defect | 152 | Defective container<br>Defective Container<br>Defective container: cracked/broken cartridges |
| `delivery_system_defect` — Delivery system defect | 136 | Defective Delivery System: Out of specification for mechanical peel and shear.<br>Defective Delivery System: There is a potential for some tablets to be missing the laser drilling which might affect drug release.<br>Miscalibrated and/or Defective Delivery System: Out of Specification results for mechanical peel force and/or the z-statistic value which relates to the patient's ability to remove the release liner from the patch adhesive prior to administration. |

### regulatory_status — Regulatory status (~528)

Recall reasons describing products marketed without the required approval, application, or regulatory clearance.

| Node | Approx recalls | Sample reasons |
| --- | ---: | --- |
| `unapproved_drug` — Unapproved drug | 528 | Marketed Without An Approved NDA/ANDA<br>Marketed without an Approved NDA/ANDA; IM and SQ injectable products are being recalled because the manufacturing firm is not registered with the FDA as a drug manufacturer<br>Marketed Without An Approved NDA/ANDA: Products marked as dietary supplements have labeling that bears drug/disease claims, making them unapproved drugs. |

### storage_distribution — Storage and distribution (~61)

Recall reasons describing temperature abuse, storage, shipment, or distribution conditions that may affect product quality.

| Node | Approx recalls | Sample reasons |
| --- | ---: | --- |
| `temperature_or_storage_abuse` — Temperature or storage abuse | 61 | Temperature Abuse: product samples were stored at temperatures below 32* F which is not in accordance with storage requirements that could cause a lack of efficacy and damage to the cartridge and pen-injectors.<br>Temperature Abuse: Product exposed to temperature outside specified limits.<br>Temperature Abuse; various products were not stored at Controlled Room Temperature as per USP guidelines during shipping |

### other — residual / uncategorized (~2,116)

Reasons that did not match the draft prefix rules. This bucket should be reviewed before a human freeze; several examples suggest either multi-label behavior or additional sterility aliases may be needed.

Examples:

- The firm received seven reports of adverse reactions in the form of skin abscesses potentially linked to compounded preservative-free methylprednisolone 80mg/ml 10 ml vials.
- Lack of Assurance of Sterility and Stability Data does not Support Expiry: recent inspection observations associated with certain quality control procedures that present a risk to sterility and quality assurance.
- Lack of Assurance Sterility: Firm is recalling various drug products due to a non-approved method of sterilization.

## Review notes

- The no-LLM draft is intentionally conservative and evidence-derived. It groups recurring openFDA prefixes into broad parent nodes and child nodes, but it is not a human-frozen taxonomy.
- The distribution is a review aid, not authoritative classification. It assigns each distinct reason text to at most one node by prefix; true closed-set LLM labeling may produce different counts and should happen only after human freeze.
- The largest residual review target is `other` (~2,116 recalls). Before freezing v1, consider adding aliases/rules for mixed sterility+stability reasons and adverse-reaction/clinical-report reasons, or explicitly deciding they remain residual.
