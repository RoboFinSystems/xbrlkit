# Parse — Arelle in, `XbrlModel` out

One filing, parsed once, into the neutral typed model every other component
reads. Arelle stays the parser: nobody should reimplement DTS resolution. What
it does not give you is anything ergonomic to *hold* — `ModelXbrl` is a large
mutable object graph tied to a controller you have to close — and this package
is the answer to that.

```python
from xbrlkit.parse import load_model, to_xbrl_model, close

mx = load_model("mmm-20241231.htm")
model = to_xbrl_model(mx, filing_meta)
close(mx.modelManager.cntlr)
```

| Module | What it does |
| --- | --- |
| `arelle_load.py` | Builds a headless Arelle controller (inline XBRL on, SEC transforms registered, the cache policy below) and loads a document. Raises `DtsResolutionError` when part of the DTS could not be resolved |
| `to_model.py` | Walks the loaded `ModelXbrl` into [`XbrlModel`](../model.py) — every fact (numeric and text), every concept in the DTS, every network, dimensional qualifiers as first-class objects |
| `cache.py` | The Arelle web cache: status, download, bundle, extract |
| `ids.py` | Deterministic UUID5 ids, so a period or a unit resolves to the same id in every run, process and machine |

Periods are built by [`xbrlkit.periods`](../periods.py) rather than here,
because three sources mint them — this parse and the two
[importers](../deserialize/README.md) — and a period's id is content-derived,
so all three have to agree exactly.

## Conventions worth knowing

- **Numeric ⇔ the fact carries a unit**, not the concept's declared type. That
  is XBRL's own convention and the platform's.
- **Arelle stores an instant or an end date as the exclusive next midnight**,
  so every one is rolled back a day to recover the date the filing reported.
- **Concept coverage is DTS-wide, not fact-driven**: a `Concept` exists for
  every qname the slice touches — abstract headers, subtotals, axes, domains,
  members — so labels and structural flags are available for all of them.

## Facts the walk cannot place

A fact is left out of the model for three reasons, and each is counted on
`model.parse_report` (by reason and context id, with a sample of the concepts)
and logged as a warning, never dropped silently:

| Reason | What happened |
| --- | --- |
| `undeclared_context` | The fact's `contextRef` names an id the instance never declares. Arelle loads the fact with no context |
| `undefined_concept` | The fact has no concept behind it |
| `invalid_period` | The context's dates could not be read |

`parse_report` is `None` when there was nothing to report.

**An undeclared context is rebuilt when the filing states its period itself.**
Some Indian exchange results filings put most of their facts under `OneD` (the
quarter) and `FourD` (the year to date) and declare neither, but report
`DateOfStartOfReportingPeriod` and `DateOfEndOfReportingPeriod` under each. When
exactly one start and one end date sit under an undeclared id, the parse builds
a duration from them and keeps the facts. The rules are narrow on purpose:

- The period comes only from those two facts, never from a declared sibling
  context: the same filings date some of those wrongly.
- The entity is the one every declared context names. If they disagree, nothing
  is rebuilt; if the instance declares none, the caller's filer id stands in.
- Only duration concepts are placed, and a rebuilt context has no dimensions.

A fact kept this way carries `provenance.kind == "recovered_context"`, which
the holon and TAVI projections write and their readers restore, and the
context is listed in `parse_report.recovered_contexts`. Arelle hashes a fact
with its context, so these facts' ids fold in the context id.

### Contexts the filing dates wrongly

Some of the same filings declare a `Four<X>D` context, a year-to-date
breakdown, with the quarter's dates. These facts are in the model either way;
what the parse may change is their period, never a value. A context is a
candidate when its dates are exactly those of `OneD` and the `FourD` period is
longer. It does not need a `One<X>D` sibling.

Candidates that share a numeric concept, directly or through one another, are
one group, and a group is re-dated to the `FourD` period or left as filed as a
whole. It is re-dated only when every context in it reports a numeric concept
and, for each concept, the members sum to the `FourD` total of that same concept
with at least one member that is not zero. No sum is taken across concepts.

A re-dated fact carries `provenance.kind == "corrected_context"`, which the
holon and TAVI projections write and their readers restore.
`parse_report.corrected_contexts` lists each context with the dates used, the
dates filed and its `One<X>D` sibling if there is one. A group left as filed is
listed in `parse_report.unverified_contexts`, each context with a reason:

| Reason | Why it was left |
| --- | --- |
| `sum_mismatch` | Its members do not sum to the `FourD` total |
| `no_total` | There is no `FourD` total of that concept |
| `all_zero` | Every member is zero, so a zero total proves nothing |
| `no_numeric_facts` | There is nothing to sum |
| `group_not_tied` | Its own concepts tie, but it is linked by shared concepts to a context that fails |

## The Arelle cache

Arelle resolves a filing's DTS by fetching every schema and linkbase it
imports — the XBRL core from xbrl.org, the W3C schemas from w3.org, `dei` /
`srt` / `ecd` / country / currency from xbrl.sec.gov, the us-gaap year from
xbrl.fasb.org. A 10-K resolves to a few hundred files, and the two smallest
hosts throttle a cold cache within a few dozen filings.

So `load_model` serves the DTS from a persistent cache
(`~/.cache/xbrlkit/arelle`, or `$XBRLKIT_ARELLE_CACHE_DIR`) in Arelle's own
layout, spaces its fetches per host, waits out a `Retry-After` on a 429 or 503,
and — when a document still cannot be resolved — raises `DtsResolutionError`
naming the URLs rather than returning a filing that parses with holes.

The one exception is a host known to be gone (`GONE_TAXONOMY_HOSTS`: the Danish
Business Authority's, which every Danish ESEF report cites beside its own
taxonomy). The filing loads without that taxonomy's concepts, a warning says
so, and the URL stays on `load_state(cntlr).unresolved` for the caller to report.

Warm the cache once, or ship it:

```bash
xbrlkit cache status                          # what the cache holds; exit 1 if unseeded
xbrlkit cache download --years 2022-2026      # load the standard entry points through Arelle
xbrlkit cache bundle --out schemas.tar.gz --host www.xbrl.org --host www.w3.org
xbrlkit cache extract --bundle schemas.tar.gz # seed a container's cache at build time
```

`XBRLKIT_ARELLE_OFFLINE=1` (or `load_model(..., offline=True)`) never touches
the network; a miss is then an error, not a fetch.

## Hosting your own controller

A host that loads filings through its own Arelle controller takes two things
from here instead of carrying the EDGAR plugin itself:

```python
from xbrlkit.parse import configure_webcache, register_sec_transforms

configure_webcache(cntlr, cache_dir)   # the same cache, spacing and backoff
register_sec_transforms()              # the SEC inline-XBRL transforms this package vendors
```

## Taxonomy packages

A filing from outside the SEC usually ships as a **taxonomy package**, because
it references the filer's extension taxonomy at their own domain. Point at the
`.zip` or the unpacked directory and the package's catalog is registered, so
those URLs resolve to the schema travelling beside the report. Register the
*archive*, not its manifest: one Dutch package's manifest raises inside Arelle
where the same package as a zip registers cleanly.

A package is registered for the one load. Arelle keeps its registrations for
the life of the process, so `load_model` clears them first: a later filing
never resolves through an earlier filing's catalog.
