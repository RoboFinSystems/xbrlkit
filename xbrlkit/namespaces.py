"""The IRIs this package mints, in the three classes that change independently.

They all currently sit under ``robosystems.ai``, which hides the fact that they
are three different kinds of thing with three different futures. Collecting
them here means a change to one cannot silently drag the others along — a
blanket find-and-replace over the domain would, and that is the mistake this
module exists to prevent.

**1. Holon vocabulary** — :data:`HOLON_VOCAB`. The predicate namespace for the
holon serialization (``rs:Fact``, ``rs:element``, …). This is the one
standardisation candidate: the holon is a xbrlkit-defined serialization today,
but it is intended to stand alongside XBRL, iXBRL and TAVI as ``xbrl-holon`` /
``xbrl-jsonld`` / ``xbrl-rdf``. If that happens, this constant becomes the
standard namespace and **nothing else here changes**.

**2. Taxonomy namespaces** — ``rs-gaap``, ``fac-traits`` and friends, defined in
``serialize/_kernel/context.py``. These name *our* reporting frameworks. They
are deliberately not re-exported here, because they are ours permanently and
must not move when the vocabulary does.

**3. Instance identity** — :data:`FACTSET_BASE`, :data:`REPORT_BASE`,
:data:`ENTITY_SCHEME`, :data:`TAVI_REPORT_BASE`. These name objects *inside one
converted filing* — this report, this fact set, this minted TAVI fact. They
identify the converter's output rather than any shared vocabulary, so they stay
attributable even if the holon standardises. Two systems converting the same
filing should agree on them, which is why they are stable and content-derived
rather than random.
"""

from __future__ import annotations

# --- 1. Holon vocabulary (the standardisation candidate) ---------------------

HOLON_VOCAB = "https://robosystems.ai/vocab/"
# PROV-O, the W3C provenance vocabulary: the holon writes a fact's provenance
# with it (`prov:hadPrimarySource`, `prov:wasAttributedTo`) rather than with
# terms of our own.
PROV_VOCAB = "http://www.w3.org/ns/prov#"

# --- 3. Instance identity (names objects within one converted filing) --------

_INSTANCE_BASE = "https://robosystems.ai"

FACTSET_BASE = f"{_INSTANCE_BASE}/factset/"
REPORT_BASE = f"{_INSTANCE_BASE}/report/"
CONCEPT_BASE = f"{_INSTANCE_BASE}/concept/"
MEASURE_BASE = f"{_INSTANCE_BASE}/measure/"
SNAPSHOT_BASE = f"{_INSTANCE_BASE}/snapshot/"
DATATYPE_BASE = f"{_INSTANCE_BASE}/datatype/v1/"
TAVI_REPORT_BASE = f"{_INSTANCE_BASE}/tavi/report"

# Kept on http:// because it is an XBRL entity *scheme* identifier that appears
# in already-emitted documents; changing the scheme changes entity identity.
ENTITY_SCHEME = "http://robosystems.ai/entity"

__all__ = (
  "PROV_VOCAB",
  "CONCEPT_BASE",
  "DATATYPE_BASE",
  "ENTITY_SCHEME",
  "FACTSET_BASE",
  "HOLON_VOCAB",
  "MEASURE_BASE",
  "REPORT_BASE",
  "SNAPSHOT_BASE",
  "TAVI_REPORT_BASE",
)
