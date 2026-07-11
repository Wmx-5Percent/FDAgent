# Taxonomy induction notes — draft v1 dry run

Issue #20 dry-run deliverable. No `--apply` command was run; no `taxonomy` rows were frozen/seeded and no `recall_label` rows were written.

## Run summary

- Source: `drug_enforcement.reason_for_recall`
- Input size: 17,723 recall records with 4,390 distinct non-empty reason texts
- Induction command: `src/classify/induce.py --output-file data/processed/taxonomy_draft_v1.json`
- LLM path result: succeeded after `.env` was copied into this worktree
- Provider/model: OpenRouter `deepseek/deepseek-v4-pro`
- Draft artifact: `data/processed/taxonomy_draft_v1.json` (git-ignored)
- Label-distribution artifact: `data/processed/taxonomy_draft_v1_label_distribution.json` (git-ignored)
- Distribution method: `label.py --taxonomy-file data/processed/taxonomy_draft_v1.json --draft-prefix-match`; dry-run prefix matching only, no LLM/cache/DB writes
- ADR note: `docs/adr/0005` was not present in this checkout; issue #20's reversible-only rules were followed.

Counts below are approximate draft-distribution counts from prefix matching against the LLM-induced draft, not frozen taxonomy labels.

## Draft two-level node list and distribution

### sterility_issues — Sterility Issues (~6,104)

Recalls due to lack of sterility assurance, non-sterility, or processing controls that compromise sterility of sterile products.

| Node | Approx recalls | Sample reasons |
| --- | ---: | --- |
| `lack_of_sterility_assurance` — Lack of Sterility Assurance | 5,949 | Lack of Assurance of Sterility<br>Lack of sterility assurance.<br>Lack of Assurance of Sterility; FDA inspection identified GMP violations potentially impacting product quality and sterility |
| `non_sterility` — Non-Sterility | 155 | Non-Sterility<br>Non-Sterility: Out of specification results for the sterility test for microbial contamination.<br>Non-Sterility: FDA found insanitary conditions and positive bacterial test results from environmental sampling at the manufacturing facility. |

### cgmp_deviations — CGMP Deviations (~3,199)

Recalls due to deviations from current good manufacturing practices, including general GMP violations, temperature excursions, and nitrosamine impurities.

| Node | Approx recalls | Sample reasons |
| --- | ---: | --- |
| `cgmp_deviation` — CGMP Deviation | 3,199 | CGMP Deviations<br>CGMP Deviations: Intermittent exposure to temperature excursion during storage.<br>cGMP deviations |

### contamination — Contamination (~2,218)

Recalls due to chemical, microbial, or foreign material contamination, including cross-contamination.

| Node | Approx recalls | Sample reasons |
| --- | ---: | --- |
| `microbial_contamination_non_sterile` — Microbial Contamination of Non-Sterile Products | 678 | Microbial contamination<br>Microbial Contamination of Non-Sterile Products<br>Microbial Contamination of Non-Sterile Products: Product is being recalled due to possible microbial contamination by C. difficile discovered in the raw material. |
| `cross_contamination` — Cross Contamination | 674 | Penicillin Cross Contamination: All lots of all products repackaged and distributed between 01/05/12 and 02/12/15 are being recalled because they were repackaged in a facility with penicillin products without adequate separation which could introduce the potential for cross contamination with penicillin.<br>Cross Contamination With Other Products: Oral care solutions were manufactured by a third party supplier on equipment shared with non-pharmaceutical products<br>Penicillin Cross Contamination |
| `particulate_matter` — Particulate Matter | 430 | Presence of Particulate Matter: API contaminated with glass particulate was used to produce sterile injectable drugs.<br>Presence of Particulate matter: manufacturer recalled fentanyl API due to potential for glass particules<br>Presence of Particulate Matter |
| `chemical_contamination` — Chemical Contamination | 229 | Chemical contamination: product contains elevated levels of undeclared lead.<br>Chemical contamination: product found to be contaminated with methanol (wood alcohol), benzene and acetaldehyde.<br>Chemical Contamination: Novartis Pharmaceuticals Corporation has recalled physician sample bottles of Diovan, Exforge, Exforge HCT,Lescol XL, Stalevo, Tekturna and Tekturna HCT Tablets due to contamination with Darocur 1173 a photocuring agent used in inks on shrink-wrap sleeves. |
| `foreign_substance` — Foreign Substance | 207 | Presence of Foreign substance - potential presence of metal particulate matter<br>Presence of foreign substance: small metallic particles in chewable tablets.<br>Presence of Foreign Substance: The products are being recalled because they may contain foreign substances. |

### labeling_packaging — Labeling and Packaging (~1,744)

Recalls due to labeling errors, packaging defects, product mix-ups, or incorrect expiration dates.

| Node | Approx recalls | Sample reasons |
| --- | ---: | --- |
| `labeling_error` — Labeling Error | 1,271 | Labeling: Not Elsewhere Classified: Products may contain synthetic latex and/or natural latex.<br>Labeling: Incorrect or Missing Lot and/or Exp Date: Beyond Use Date (BUD) exceed the BUD/EXP of at least one ingredient used to make final product.<br>Labeling: Incorrect or missing lot and/or expiration date. Products were compounded using expired components. |
| `packaging_defect` — Packaging Defect | 302 | Defective container<br>Defective Container<br>Defective container: cracked/broken cartridges |
| `product_mixup` — Product Mix-Up | 128 | Presence of Foreign Tablets/Capsules<br>Failed Excipient Specifications and Presence of Foreign Tablets/Capsules; product manufactured using an excipient found to be OOS for conductivity and some Ezetimibe and Simvastatin Tablets, 10 mg/10 mg were found in the bottle<br>Incorrect Product Formulation |
| `incorrect_expiration_date` — Incorrect Expiration Date | 43 | Labeling Incorrect Instructions: This recall has been initiated because the Instructions for Use included in the Epi-Safe Kits for the Epi-Safe Syringe recommend an unapproved midpoint dosage of epinephrine for children.<br>Labeling Not Elsewhere Classified: Misbranding.<br>Labeling Product Contains Undeclared API: Active Ingredient on label is not the active ingredient in the product. |

### product_specification_failure — Product Specification Failure (~2,084)

Recalls due to failure to meet product quality specifications such as potency, impurities, dissolution, stability, or physical defects.

| Node | Approx recalls | Sample reasons |
| --- | ---: | --- |
| `potency_failure` — Potency Failure | 681 | Subpotent Drug<br>Subpotent Drug:The titratable iodine contained in the Povidone-Iodine prep pads is below label claim of 0.85%.<br>Subpotent drug |
| `impurity_degradation` — Impurity/Degradation Failure | 547 | Failed Impurities/Degradation Specifications<br>Failed Impurities/Degradation Specifications; Out of specification result obtained for impurity A during stability testing.<br>Failed Impurities/Degradation Specifications. |
| `dissolution_failure` — Dissolution Failure | 364 | Failed Dissolution Specifications<br>Failed dissolution specifications<br>Failed Dissolution Specification |
| `stability_failure` — Stability Failure | 222 | Stability Data Does Not Support Expiry: potential loss of potency in drugs packaged and stored in syringes.<br>Failed Stability Specifications<br>Stability data does not support expiry. |
| `physical_defect` — Physical Defect | 192 | Crystallization<br>Discoloration<br>Failed Tablet/Capsule Specifications |
| `other_specification_failure` — Other Specification Failure | 78 | Failed Content Uniformity Specifications<br>Failed Content Uniformity Specifications: Product was manufactured using an adulterated active pharmaceutical ingredient; additionally, lack of process controls and good manufacturing practices resulted in finished product failing content uniformity specifications.<br>Failed Content Uniformity Specifications: The product may not meet the limit for blend uniformity specification. |

### unapproved_misbranded — Unapproved/Misbranded Products (~544)

Recalls due to marketing without approved NDA/ANDA, failure to meet OTC monograph, or misbranding as unapproved new drugs.

| Node | Approx recalls | Sample reasons |
| --- | ---: | --- |
| `unapproved_drug` — Unapproved New Drug | 530 | Marketed Without An Approved NDA/ANDA<br>Marketed without an Approved NDA/ANDA; IM and SQ injectable products are being recalled because the manufacturing firm is not registered with the FDA as a drug manufacturer<br>Marketed Without An Approved NDA/ANDA: Products marked as dietary supplements have labeling that bears drug/disease claims, making them unapproved drugs. |
| `monograph_noncompliance` — Monograph Noncompliance | 14 | Does Not Meet Monograph: NEW GPC INC. has recalled multiple Over-the-Counter Drug Products due to lack of drug listing, lack of OTC drug labeling requirements and labeled Not Approved for sale in U.S.A..<br>Does Not Meet Monograph: Budesonide may be slightly above or below the specification range.<br>Does Not Meet Monograph: Phenylephrine and pseudoephedrine are below monograph specifications, and label inaccurately contains wording "Rx Only". |

### other — residual / uncategorized (~1,756)

Recall reasons not covered by the above categories. This bucket should be reviewed before a human freeze; several examples suggest either additional alias handling or deliberate residual policy is needed.

Examples:

- The firm received seven reports of adverse reactions in the form of skin abscesses potentially linked to compounded preservative-free methylprednisolone 80mg/ml 10 ml vials.
- Lack of Processing Controls.
- Lack of Processing Controls

## Review notes

- This is the LLM-induced draft after `.env` was added to the worktree. It is more granular than the prior no-LLM prefix fallback and introduces separate parents for sterility, CGMP, contamination, labeling/packaging, product specification failure, and unapproved/misbranded products.
- The distribution is a review aid, not authoritative classification. It assigns each distinct reason text to at most one node by prefix against the draft taxonomy; true closed-set LLM labeling may produce different counts and should happen only after human freeze.
- The largest residual review target is `other` (~1,756 recalls). Before freezing v1, consider whether `processing_controls` should be restored as a child node, whether mixed sterility/process-control reasons need aliases, and whether adverse-reaction reports should remain residual.
