"""Frozen v1 recall-reason taxonomy nodes for offline classification."""
from __future__ import annotations

from dataclasses import dataclass

VERSION = "v1"


@dataclass(frozen=True)
class TaxonomyNodeSpec:
    node_id: str
    parent_id: str | None
    label: str
    definition: str
    examples: tuple[str, ...]
    level: int


NODES: tuple[TaxonomyNodeSpec, ...] = (
    TaxonomyNodeSpec(
        node_id="quality_and_potency",
        parent_id=None,
        label="Quality and potency",
        definition="Recall reasons describing failed product quality attributes, potency, specifications, contamination, sterility, or stability.",
        examples=("Subpotent Drug", "Failed Dissolution Specifications", "Failed Impurities/Degradation Specifications"),
        level=0,
    ),
    TaxonomyNodeSpec(
        node_id="manufacturing_controls",
        parent_id=None,
        label="Manufacturing controls",
        definition="Recall reasons describing manufacturing practice, process-control, or cross-contamination control failures.",
        examples=("cGMP Deviations", "Lack of Processing Controls", "Cross Contamination with Other Products"),
        level=0,
    ),
    TaxonomyNodeSpec(
        node_id="labeling_and_packaging",
        parent_id=None,
        label="Labeling and packaging",
        definition="Recall reasons describing labeling, package, container, closure, or delivery-system defects.",
        examples=("Labeling", "Defective Container", "Defective Delivery System"),
        level=0,
    ),
    TaxonomyNodeSpec(
        node_id="regulatory_status",
        parent_id=None,
        label="Regulatory status",
        definition="Recall reasons describing products marketed without the required approval, application, or regulatory clearance.",
        examples=("Marketed Without an Approved NDA/ANDA", "Marked without an Approved NDA/ANDA"),
        level=0,
    ),
    TaxonomyNodeSpec(
        node_id="storage_distribution",
        parent_id=None,
        label="Storage and distribution",
        definition="Recall reasons describing temperature abuse, storage, shipment, or distribution conditions that may affect product quality.",
        examples=("Temperature Abuse",),
        level=0,
    ),
    TaxonomyNodeSpec(
        node_id="other",
        parent_id=None,
        label="Other",
        definition="Recall reasons that do not clearly fit the current v1 taxonomy or need human review before classification.",
        examples=(),
        level=0,
    ),
    TaxonomyNodeSpec(
        node_id="sterility_assurance",
        parent_id="quality_and_potency",
        label="Sterility assurance",
        definition="Sterile or intended-sterile products recalled for lack of sterility assurance, non-sterility, or sterility-process concerns.",
        examples=("Lack of Assurance of Sterility", "Lack of Sterility Assurance", "Non-Sterility"),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="microbial_contamination",
        parent_id="quality_and_potency",
        label="Microbial contamination",
        definition="Recall reasons describing actual or potential microbial contamination in sterile or non-sterile products.",
        examples=("Microbial Contamination of Non-Sterile Products",),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="particulate_or_foreign_matter",
        parent_id="quality_and_potency",
        label="Particulate or foreign matter",
        definition="Recall reasons describing particulate matter, foreign substances, or foreign tablets/capsules in the product.",
        examples=("Presence of Particulate Matter", "Presence of Foreign Substance", "Presence of Foreign Tablets/Capsules"),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="impurities_or_degradation",
        parent_id="quality_and_potency",
        label="Impurities or degradation",
        definition="Recall reasons describing failed impurity, degradation, chemical contamination, or related chemistry specifications.",
        examples=("Failed Impurities/Degradation Specifications", "Chemical Contamination"),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="potency_or_content",
        parent_id="quality_and_potency",
        label="Potency or content",
        definition="Recall reasons describing subpotent, superpotent, content-uniformity, or assay-strength failures.",
        examples=("Subpotent Drug", "Superpotent Drug", "Failed Content Uniformity Specifications"),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="dissolution_or_tablet_specs",
        parent_id="quality_and_potency",
        label="Dissolution or tablet specifications",
        definition="Recall reasons describing failed dissolution, tablet, capsule, crystallization, or other physical dosage-form specifications.",
        examples=("Failed Dissolution Specifications", "Failed Tablet/Capsule Specifications", "Crystallization"),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="stability_or_expiry",
        parent_id="quality_and_potency",
        label="Stability or expiry",
        definition="Recall reasons describing failed stability specifications or insufficient data to support expiration dating.",
        examples=("Stability Data Does Not Support Expiry", "Failed Stability Specifications"),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="appearance_or_physical_defect",
        parent_id="quality_and_potency",
        label="Appearance or physical defect",
        definition="Recall reasons describing discoloration or visible physical defects not better captured by foreign matter or dosage-form specifications.",
        examples=("Discoloration",),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="formulation_or_ingredient_error",
        parent_id="quality_and_potency",
        label="Formulation or ingredient error",
        definition="Recall reasons describing incorrect, undeclared, wrong-grade, or substituted active/inactive ingredients or excipients.",
        examples=("Incorrect/Undeclared Excipients", "Product was manufactured with Potassium Chloride instead of Potassium Phosphate"),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="other_specification_failure",
        parent_id="quality_and_potency",
        label="Other specification failure",
        definition="Recall reasons describing failed or out-of-specification quality tests not captured by potency, dissolution, impurity, stability, or appearance categories.",
        examples=("Out-of-Specification test results", "Failed Moisture Limits", "Does Not Meet Monograph"),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="cgmp_deviation",
        parent_id="manufacturing_controls",
        label="cGMP deviation",
        definition="Recall reasons describing cGMP/GMP deviations or broad manufacturing-quality-system failures.",
        examples=("cGMP Deviations", "cGMP Deviation", "GMP Deviations"),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="processing_controls",
        parent_id="manufacturing_controls",
        label="Processing controls",
        definition="Recall reasons describing missing, inadequate, or failed production/process controls.",
        examples=("Lack of Processing Controls",),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="cross_contamination",
        parent_id="manufacturing_controls",
        label="Cross contamination",
        definition="Recall reasons describing cross-contamination between products, ingredients, or drug classes, including penicillin cross-contamination.",
        examples=("Penicillin Cross Contamination", "Cross Contamination with Other Products"),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="labeling_error",
        parent_id="labeling_and_packaging",
        label="Labeling error",
        definition="Recall reasons describing incorrect, missing, misleading, or otherwise defective labeling.",
        examples=("Labeling",),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="container_or_closure_defect",
        parent_id="labeling_and_packaging",
        label="Container or closure defect",
        definition="Recall reasons describing defective containers, closures, packaging integrity, or similar package failures.",
        examples=("Defective Container",),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="delivery_system_defect",
        parent_id="labeling_and_packaging",
        label="Delivery system defect",
        definition="Recall reasons describing defective drug delivery devices or delivery-system components.",
        examples=("Defective Delivery System",),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="unapproved_drug",
        parent_id="regulatory_status",
        label="Unapproved drug",
        definition="Products recalled because they were marketed without an approved NDA/ANDA or equivalent required approval.",
        examples=("Marketed Without an Approved NDA/ANDA",),
        level=1,
    ),
    TaxonomyNodeSpec(
        node_id="temperature_abuse",
        parent_id="storage_distribution",
        label="Temperature abuse",
        definition="Recall reasons describing exposure to improper temperatures during storage, shipping, or distribution.",
        examples=("Temperature Abuse",),
        level=1,
    ),
)


def nodes_by_id() -> dict[str, TaxonomyNodeSpec]:
    return {node.node_id: node for node in NODES}


def validate() -> None:
    node_ids = set()
    for node in NODES:
        if node.node_id in node_ids:
            raise ValueError(f"duplicate node_id {node.node_id!r}")
        node_ids.add(node.node_id)
    by_id = nodes_by_id()
    for node in NODES:
        if node.level == 0 and node.parent_id is not None:
            raise ValueError(f"root node {node.node_id!r} must not have parent_id")
        if node.level > 0:
            if node.parent_id is None:
                raise ValueError(f"child node {node.node_id!r} must have parent_id")
            if node.parent_id not in by_id:
                raise ValueError(f"child node {node.node_id!r} references unknown parent_id {node.parent_id!r}")
