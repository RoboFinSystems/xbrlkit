"""Canonical JSON-LD @context for the taxonomy library.

The same context is used by the serializer (rdflib.Graph → JSON-LD) and
the loader (JSON-LD → rdflib.Graph → TaxonomyPackage) to ensure
consistent IRI prefixes and predicate names across every seed artifact.

Predicate design:
- Standard RDF/XBRL predicates use their canonical IRIs (rdfs:label,
  skos:altLabel, owl:equivalentClass, etc).
- RoboSystems-specific predicates use the `rs:` prefix
  (https://robosystems.ai/vocab/).
- Taxonomy-specific prefixes (fac, rs-gaap, us-gaap, …) point at the
  authoritative namespaces used by Charlie Hoffman and FASB.
"""

from __future__ import annotations

from ...namespaces import HOLON_VOCAB

# Base IRI for the holon's own predicates. See namespaces.py: this is the one
# namespace here that would change if the holon standardises; the taxonomy
# prefixes below are ours permanently and must not move with it.
RS_VOCAB = HOLON_VOCAB

# Canonical @context as a Python dict. Serialized directly to JSON-LD.
CANONICAL_CONTEXT: dict = {
  # RDF / semantic web
  "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
  "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
  "skos": "http://www.w3.org/2004/02/skos/core#",
  "owl": "http://www.w3.org/2002/07/owl#",
  "xsd": "http://www.w3.org/2001/XMLSchema#",
  "dcterms": "http://purl.org/dc/terms/",
  # XBRL core. The linkbase namespace is bound to `link` (XBRL-conventional
  # and matching the export bundle); `xlink` + `xbrldi` are bound so reified
  # arcs and dimensional members compact cleanly. `iso4217` is bound for
  # instance-side unit measures.
  "xbrli": "http://www.xbrl.org/2003/instance#",
  "link": "http://www.xbrl.org/2003/linkbase#",
  "xlink": "http://www.w3.org/1999/xlink#",
  "xbrldt": "http://xbrl.org/2005/xbrldt#",
  "xbrldi": "http://xbrl.org/2006/xbrldi#",
  "iso4217": "http://www.xbrl.org/2003/iso4217#",
  # Taxonomy namespaces (external authorities).
  # XBRL schemas use '#' fragment separator between the targetNamespace
  # and the local element name, so concept IRIs need the '#' in the
  # prefix mapping to compact correctly.
  # Charlie publishes FAC under multiple target namespaces across
  # iterations. `fac` is pinned to the 2021/kg mapping variant since
  # that's the ingest target for the POC; `fac-luca` and
  # `fac-seattlemethod` are retained so concepts authored against those
  # older variants still compact to readable qnames.
  "fac": "http://www.xbrlsite.com/fac#",
  "fac-luca": "http://luca.auditchain.finance/fac#",
  "fac-seattlemethod": "http://xbrlsite.azurewebsites.net/seattlemethod/fac#",
  "us-gaap-2017": "http://fasb.org/us-gaap/2017-01-31#",
  "us-gaap-2020": "http://fasb.org/us-gaap/2020-01-31#",
  "us-gaap-2022": "http://fasb.org/us-gaap/2022-01-31#",
  "us-gaap-2024": "http://fasb.org/us-gaap/2024-01-31#",
  "us-gaap": "http://fasb.org/us-gaap/",
  # srt — the SEC Reporting Taxonomy (ConsolidationItemsAxis, range axes,
  # geographical axes, …). Year-normalized like us-gaap, so srt concepts get
  # their real FASB namespace instead of the robosystems concept fallback.
  "srt": "http://fasb.org/srt/",
  # rs-gaap — RoboSystems's year-independent canonical reporting
  # taxonomy. Our namespace for concepts that previously lived under
  # us-gaap-2017; equivalence arcs bridge rs-gaap ↔ external us-gaap
  # versions, keeping our namespace stable as FASB evolves.
  "rs-gaap": "https://robosystems.ai/taxonomy/rs-gaap/v1/",
  # rs-gaap-disclosures — named Disclosures (BalanceSheet, IncomeStatement,
  # PropertyPlantAndEquipmentDisclosure, …) anchored to the rs-gaap framework.
  # Each entry is an abstract qname-addressable element AND a Structure with
  # CAP + secType metadata. Sibling namespace to rs-gaap, not nested under it.
  "disclosures": "https://robosystems.ai/taxonomy/rs-gaap/disclosures/v1/",
  # rs-gaap-reporting-checklist — declares the abstract "FinancialReport"
  # subjects that a Reporting Checklist's `financialReport-requiresDisclosure`
  # arcs anchor on.
  "checklist": "https://robosystems.ai/taxonomy/rs-gaap/reporting-checklist/v1/",
  # rs-gaap-reporting-styles — declares Style entities that compose specific
  # Disclosures for a vertical / filer profile.
  "styles": "https://robosystems.ai/taxonomy/rs-gaap/reporting-styles/v1/",
  "ifrs": "http://xbrl.ifrs.org/taxonomy/",
  "dei": "http://xbrl.sec.gov/dei/",
  # Seattle Method conceptual-model role URIs (Charlie's CM namespace)
  "cm-roles": "http://www.xbrlsite.com/seattlemethod/conceptual-model/cm-roles/roles/",
  # cm — Seattle Method 'universal' conceptual model (Charlie Hoffman). The
  # Debit/Credit posting-role concepts anchor has-part arcs from Chart-of-
  # Accounts elements. '#' fragment separator so concept IRIs compact to
  # cm:Debit / cm:Credit.
  "cm": "https://github.com/seattlemethod/universal/cm#",
  # RoboSystems vocabulary
  "rs": RS_VOCAB,
  # Concept attributes — XBRL vocabulary where XBRL defines the attribute
  # (balance, periodType), rs: for our denormalized booleans/axes that XBRL
  # has no predicate for (monetary, abstract, elementType, classification …).
  "classification": {"@id": f"{RS_VOCAB}classification"},
  "statementContext": {"@id": f"{RS_VOCAB}statementContext"},
  "derivationRole": {"@id": f"{RS_VOCAB}derivationRole"},
  "balance": {"@id": "xbrli:balance"},
  "periodType": {"@id": "xbrli:periodType"},
  "abstract": {"@id": f"{RS_VOCAB}abstract", "@type": "xsd:boolean"},
  "monetary": {"@id": f"{RS_VOCAB}monetary", "@type": "xsd:boolean"},
  "elementType": {"@id": f"{RS_VOCAB}elementType"},
  # v1.1 — the element's value domain (textBlock / monetary / shares / decimal /
  # date / boolean / string), orthogonal to elementType's structural role.
  "itemType": {"@id": f"{RS_VOCAB}itemType"},
  # The type the taxonomy declares, beside the value domain `itemType` buckets
  # it into: `dei:yesNoItemType` is a `string` domain but is not a string type.
  "dataType": {"@id": f"{RS_VOCAB}dataType"},
  # The XML Schema type the declared type ultimately derives from.
  "baseType": {"@id": f"{RS_VOCAB}baseType"},
  # The period the report covers, beside the date it was filed.
  "periodEndDate": {"@id": f"{RS_VOCAB}periodEndDate", "@type": "xsd:date"},
  "nillable": {"@id": f"{RS_VOCAB}nillable", "@type": "xsd:boolean"},
  "substitutionGroup": {"@id": f"{RS_VOCAB}substitutionGroup", "@type": "@id"},
  "source": {"@id": f"{RS_VOCAB}source"},
  # Relationships. Structural taxonomy arcs (presentation / calculation /
  # definition) are REIFIED as rs:Association nodes carrying xlink:from/to +
  # xlink:arcrole + link:weight/order — the direct-predicate terms
  # (parent/summationOf/generalOf/dimensionOf/hypercubeOf) are RETIRED.
  # `equivalent` stays a direct owl:equivalentClass predicate: it is a genuine
  # symmetric OWL relation with no weight/order/role to carry, so reifying it
  # would gain nothing and lose the OWL semantics the bridges rely on.
  "equivalent": {"@id": "owl:equivalentClass", "@type": "@id"},
  # Reified-association predicates (one rs:Association node per arc)
  "from": {"@id": "xlink:from", "@type": "@id"},
  "to": {"@id": "xlink:to", "@type": "@id"},
  "arcrole": {"@id": "xlink:arcrole", "@type": "@id"},
  "role": {"@id": "xlink:role", "@type": "@id"},
  "order": {"@id": "link:order", "@type": "xsd:decimal"},
  "weight": {"@id": "link:weight", "@type": "xsd:decimal"},
  "associationType": {"@id": f"{RS_VOCAB}associationType"},
  # Per-arc preferred label: the resolved string the filer chose for this row
  # plus the label role URI (negated* roles carry the display-sign semantic).
  "preferredLabel": {"@id": f"{RS_VOCAB}preferredLabel"},
  "preferredLabelRole": {"@id": f"{RS_VOCAB}preferredLabelRole"},
  # Labels
  "label": "rdfs:label",
  "altLabel": "skos:altLabel",
  "prefLabel": "skos:prefLabel",
  "documentation": "rdfs:comment",
  # The standard-role label, written beside `skos:prefLabel` because only
  # this one can carry the language the label was authored in.
  "label": {"@id": f"{RS_VOCAB}label"},
  # Every other XBRL label role, one term each. A label is a literal with a
  # role, so it is one triple rather than a node: reifying them would add a
  # node per label, half again as many nodes as a report has.
  "deprecatedDateLabel": {"@id": f"{RS_VOCAB}deprecatedDateLabel"},
  "deprecatedLabel": {"@id": f"{RS_VOCAB}deprecatedLabel"},
  "negated": {"@id": f"{RS_VOCAB}negated"},
  "negatedLabel": {"@id": f"{RS_VOCAB}negatedLabel"},
  "negatedNetLabel": {"@id": f"{RS_VOCAB}negatedNetLabel"},
  "negatedPeriodEnd": {"@id": f"{RS_VOCAB}negatedPeriodEnd"},
  "negatedPeriodEndLabel": {"@id": f"{RS_VOCAB}negatedPeriodEndLabel"},
  "negatedPeriodStart": {"@id": f"{RS_VOCAB}negatedPeriodStart"},
  "negatedPeriodStartLabel": {"@id": f"{RS_VOCAB}negatedPeriodStartLabel"},
  "negatedTerseLabel": {"@id": f"{RS_VOCAB}negatedTerseLabel"},
  "negatedTotal": {"@id": f"{RS_VOCAB}negatedTotal"},
  "negatedTotalLabel": {"@id": f"{RS_VOCAB}negatedTotalLabel"},
  "negativeLabel": {"@id": f"{RS_VOCAB}negativeLabel"},
  "negativePeriodEndLabel": {"@id": f"{RS_VOCAB}negativePeriodEndLabel"},
  "negativePeriodEndTotalLabel": {"@id": f"{RS_VOCAB}negativePeriodEndTotalLabel"},
  "negativePeriodStartLabel": {"@id": f"{RS_VOCAB}negativePeriodStartLabel"},
  "negativePeriodStartTotalLabel": {"@id": f"{RS_VOCAB}negativePeriodStartTotalLabel"},
  "negativeTerseLabel": {"@id": f"{RS_VOCAB}negativeTerseLabel"},
  "negativeVerboseLabel": {"@id": f"{RS_VOCAB}negativeVerboseLabel"},
  "netLabel": {"@id": f"{RS_VOCAB}netLabel"},
  "periodEndLabel": {"@id": f"{RS_VOCAB}periodEndLabel"},
  "periodStartLabel": {"@id": f"{RS_VOCAB}periodStartLabel"},
  "positiveLabel": {"@id": f"{RS_VOCAB}positiveLabel"},
  "positivePeriodEndLabel": {"@id": f"{RS_VOCAB}positivePeriodEndLabel"},
  "positivePeriodEndTotalLabel": {"@id": f"{RS_VOCAB}positivePeriodEndTotalLabel"},
  "positivePeriodStartLabel": {"@id": f"{RS_VOCAB}positivePeriodStartLabel"},
  "positivePeriodStartTotalLabel": {"@id": f"{RS_VOCAB}positivePeriodStartTotalLabel"},
  "positiveTerseLabel": {"@id": f"{RS_VOCAB}positiveTerseLabel"},
  "positiveVerboseLabel": {"@id": f"{RS_VOCAB}positiveVerboseLabel"},
  "restatedLabel": {"@id": f"{RS_VOCAB}restatedLabel"},
  "terseLabel": {"@id": f"{RS_VOCAB}terseLabel"},
  "totalLabel": {"@id": f"{RS_VOCAB}totalLabel"},
  "verboseLabel": {"@id": f"{RS_VOCAB}verboseLabel"},
  "zeroLabel": {"@id": f"{RS_VOCAB}zeroLabel"},
  "zeroTerseLabel": {"@id": f"{RS_VOCAB}zeroTerseLabel"},
  "zeroVerboseLabel": {"@id": f"{RS_VOCAB}zeroVerboseLabel"},
  "labelRole": {"@id": f"{RS_VOCAB}labelRole"},
  "labelLanguage": {"@id": f"{RS_VOCAB}labelLanguage"},
  # References
  "references": {"@id": "dcterms:references"},
  "refType": {"@id": f"{RS_VOCAB}refType"},
  "citation": {"@id": f"{RS_VOCAB}citation"},
  # Structure (extended link roles)
  "structureName": {"@id": f"{RS_VOCAB}structureName"},
  # v1.1 — the filer's section sequence (leading number of a SEC role
  # definition), so consumers order all sections by filing order.
  "structureOrder": {"@id": f"{RS_VOCAB}structureOrder", "@type": "xsd:integer"},
  "blockType": {"@id": f"{RS_VOCAB}blockType"},
  "roleUri": {"@id": f"{RS_VOCAB}roleUri"},
  "conceptArrangementPattern": {"@id": f"{RS_VOCAB}conceptArrangementPattern"},
  "hasAssociation": {"@id": f"{RS_VOCAB}hasAssociation", "@type": "@id"},
  # Instance layer — Fact + its aspects, mirroring the graph's FACT_HAS_*
  # edges (Fact → Element / Entity / Period / Unit / Dimension). No XBRL
  # `context` exists here: a Fact references its aspects directly.
  "element": {"@id": f"{RS_VOCAB}element", "@type": "@id"},
  "entity": {"@id": f"{RS_VOCAB}entity", "@type": "@id"},
  "period": {"@id": f"{RS_VOCAB}period", "@type": "@id"},
  "unit": {"@id": f"{RS_VOCAB}unit", "@type": "@id"},
  "dimension": {"@id": f"{RS_VOCAB}dimension", "@type": "@id"},
  "factSet": {"@id": f"{RS_VOCAB}factSet", "@type": "@id"},
  "structure": {"@id": f"{RS_VOCAB}structure", "@type": "@id"},
  "numericValue": {"@id": f"{RS_VOCAB}numericValue", "@type": "xsd:decimal"},
  # v1.1 — non-numeric (text / textBlock) fact value + numeric/nonnumeric kind.
  # rs: has no XBRL predicate for a string-valued fact; the graph carries it as
  # Fact.value + Fact.fact_type, mirrored here so disclosures survive the slice.
  "stringValue": {"@id": f"{RS_VOCAB}stringValue"},
  "factType": {"@id": f"{RS_VOCAB}factType"},
  "decimals": {"@id": f"{RS_VOCAB}decimals"},
  # xsi:nil, and the fact's xml:lang — a fact reported as not disclosed is not
  # a fact with an empty value, and a text fact's language is part of it.
  "isNil": {"@id": f"{RS_VOCAB}isNil", "@type": "xsd:boolean"},
  "language": {"@id": f"{RS_VOCAB}language"},
  # Period node — period kind uses XBRL's instant/duration vocabulary
  "instant": {"@id": "xbrli:instant", "@type": "xsd:date"},
  "startDate": {"@id": "xbrli:startDate", "@type": "xsd:date"},
  "endDate": {"@id": "xbrli:endDate", "@type": "xsd:date"},
  "calendarPeriodKey": {"@id": f"{RS_VOCAB}calendarPeriodKey"},
  # v1.1 — derived calendar enrichment: normalize a period onto a common
  # calendar axis (year + quarter) and bucket its duration.
  "calendarYear": {"@id": f"{RS_VOCAB}calendarYear", "@type": "xsd:integer"},
  "calendarQuarter": {"@id": f"{RS_VOCAB}calendarQuarter"},
  "durationType": {"@id": f"{RS_VOCAB}durationType"},
  # Unit node
  "measure": {"@id": "xbrli:measure", "@type": "@id"},
  # Dimension node (v1.1 fidelity layer). rs:axis / rs:member are refined from
  # reserved string stubs to @id links onto the axis / member rs:Element, so
  # their labels join in-graph; typed dimensions carry rs:typedValue instead of
  # a member. rs:axisType records segment vs scenario.
  "axis": {"@id": f"{RS_VOCAB}axis", "@type": "@id"},
  "member": {"@id": f"{RS_VOCAB}member", "@type": "@id"},
  "isExplicit": {"@id": f"{RS_VOCAB}isExplicit", "@type": "xsd:boolean"},
  "isTyped": {"@id": f"{RS_VOCAB}isTyped", "@type": "xsd:boolean"},
  "typedValue": {"@id": f"{RS_VOCAB}typedValue"},
  "axisType": {"@id": f"{RS_VOCAB}axisType"},
  # Entity / report-bundle header (rs: — no XBRL equivalent)
  "scheme": {"@id": f"{RS_VOCAB}scheme", "@type": "@id"},
  "legalName": {"@id": f"{RS_VOCAB}legalName"},
  "ein": {"@id": f"{RS_VOCAB}ein"},
  "country": {"@id": f"{RS_VOCAB}country"},
  "reportingStyle": {"@id": f"{RS_VOCAB}reportingStyle"},
  "serializationVersion": {"@id": f"{RS_VOCAB}serializationVersion"},
  "mode": {"@id": f"{RS_VOCAB}mode"},
  "internalId": {"@id": f"{RS_VOCAB}internalId"},
  # v1.1 — Report-node filing metadata (aligns with the SEC graph's Report node),
  # so the holon identifies its filing rather than encoding it only in the graph IRI.
  "accessionNumber": {"@id": f"{RS_VOCAB}accessionNumber"},
  "form": {"@id": f"{RS_VOCAB}form"},
  "filingDate": {"@id": f"{RS_VOCAB}filingDate", "@type": "xsd:date"},
  "fiscalYearFocus": {"@id": f"{RS_VOCAB}fiscalYearFocus"},
  "fiscalPeriodFocus": {"@id": f"{RS_VOCAB}fiscalPeriodFocus"},
  "fiscalYearEndMonth": {"@id": f"{RS_VOCAB}fiscalYearEndMonth"},
  # ── Domain / package terms ─────────────────────────────────────────────
  # Every term any framework seed uses must live here so the one canonical
  # context is a true superset — undeclared terms would either drop on parse
  # or compact to ugly rs:-prefixed keys. These are pure binary relations
  # (drules), rule/trait/style metadata, and structure annotations; none are
  # the retired structural-arc dialect.
  "category": {"@id": f"{RS_VOCAB}category"},
  "classifiedAs": {"@id": f"{RS_VOCAB}classifiedAs", "@type": "@id"},
  "deprecated": {"@id": f"{RS_VOCAB}deprecated", "@type": "xsd:boolean"},
  "replacedBy": {"@id": f"{RS_VOCAB}replacedBy", "@type": "@id"},
  "orphan2026": {"@id": f"{RS_VOCAB}orphan2026", "@type": "xsd:boolean"},
  "identifier": {"@id": f"{RS_VOCAB}identifier"},
  "secType": {"@id": f"{RS_VOCAB}secType"},
  "hasTrait": {"@id": f"{RS_VOCAB}hasTrait", "@type": "@id"},
  "trait": "https://robosystems.ai/taxonomy/fac-traits/v1/",
  "sfac6": "http://xbrlsite.com/seattlemethod/sfac6#",
  # Disclosure / checklist / style "drules" — direct binary relations
  "reportedDisclosureRequiresDisclosure": {
    "@id": f"{RS_VOCAB}reportedDisclosureRequiresDisclosure",
    "@type": "@id",
  },
  "conceptArrangementPatternRequiresConcept": {
    "@id": f"{RS_VOCAB}conceptArrangementPatternRequiresConcept",
    "@type": "@id",
  },
  "disclosureRequiresHypercube": {
    "@id": f"{RS_VOCAB}disclosureRequiresHypercube",
    "@type": "@id",
  },
  "disclosureRequiresConcept": {
    "@id": f"{RS_VOCAB}disclosureRequiresConcept",
    "@type": "@id",
  },
  "disclosureEquivalentTextblock": {
    "@id": f"{RS_VOCAB}disclosureEquivalentTextblock",
    "@type": "@id",
  },
  "financialReportRequiresDisclosure": {
    "@id": f"{RS_VOCAB}financialReportRequiresDisclosure",
    "@type": "@id",
  },
  "financialReportPossibleDisclosure": {
    "@id": f"{RS_VOCAB}financialReportPossibleDisclosure",
    "@type": "@id",
  },
  "disclosureAllowedAlternativeDisclosure": {
    "@id": f"{RS_VOCAB}disclosureAllowedAlternativeDisclosure",
    "@type": "@id",
  },
  "reportingStyleComposesDisclosure": {
    "@id": f"{RS_VOCAB}reportingStyleComposesDisclosure",
    "@type": "@id",
  },
  # Reporting-style composition (read as raw JSON by the style seeder)
  "reportingStyleCode": {"@id": f"{RS_VOCAB}reportingStyleCode"},
  "retainedEarningsConcept": {"@id": f"{RS_VOCAB}retainedEarningsConcept"},
  "reportingStyleNetworks": {"@id": f"{RS_VOCAB}reportingStyleNetworks"},
  "statementType": {"@id": f"{RS_VOCAB}statementType"},
  "networkRoleUri": {"@id": f"{RS_VOCAB}networkRoleUri"},
  # Validation-rule terms (rs-gaap-rules / rollup-rules packages)
  "ruleTarget": {"@id": f"{RS_VOCAB}ruleTarget"},
  "ruleCategory": {"@id": f"{RS_VOCAB}ruleCategory"},
  "rulePattern": {"@id": f"{RS_VOCAB}rulePattern"},
  "ruleSeverity": {"@id": f"{RS_VOCAB}ruleSeverity"},
  "ruleExpression": {"@id": f"{RS_VOCAB}ruleExpression"},
  "ruleMessage": {"@id": f"{RS_VOCAB}ruleMessage"},
  "ruleOrigin": {"@id": f"{RS_VOCAB}ruleOrigin"},
  "ruleVariables": {"@id": f"{RS_VOCAB}ruleVariables", "@container": "@list"},
  "variableName": {"@id": f"{RS_VOCAB}variableName"},
  "variableQname": {"@id": f"{RS_VOCAB}variableQname"},
  "targetKind": {"@id": f"{RS_VOCAB}targetKind"},
  "targetRef": {"@id": f"{RS_VOCAB}targetRef"},
}


def context_document() -> dict:
  """Return a JSON-LD document with only the context (for seeds/context.jsonld).

  Consumers that import the context via a URL reference can point to this
  file. Sidecar artifact for discoverability.
  """
  return {"@context": CANONICAL_CONTEXT}
