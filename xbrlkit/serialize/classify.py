"""Classify a presentation network into a primary-statement block type.

The holon MVP keeps only the four primary financial statements; everything
else (disclosures, document/entity info, parentheticals, detail schedules) is
skipped. Classification is a case-insensitive keyword heuristic over the
network's human definition, falling back to the role URI when no definition is
present or the definition does not classify, and finally to the presentation
tree's root concept.

That last step is what makes the classifier work outside English. The keyword
table can only read a definition written in English, so an ESEF filer's
"Rapport över finansiell ställning" or "Estado de situación financiera"
classified as nothing at all and its primary statements went missing. The root
of a presentation tree carries the same meaning in a form no language touches:
IFRS and US GAAP both name it ``StatementOfFinancialPositionAbstract``, so one
table of local names covers both taxonomies.

It runs last, not first, because the root is a weaker signal than it looks: a
balance sheet and its parenthetical share a root, and so do a disclosure and
the statement it details. The definition is what separates those, so the
exclusions and the keyword table have to have their say first.
"""

from __future__ import annotations

import re
from typing import Any

# Deterministic block order — also the priority used when a definition could
# plausibly match more than one statement family.
BLOCK_TYPES: tuple[str, ...] = (
  "balance_sheet",
  "income_statement",
  "cash_flow_statement",
  "equity_statement",
)


def _match(text: str) -> str | None:
  """Return the block type for pre-normalized (lowercased) statement text."""
  if "cash flow" in text:
    return "cash_flow_statement"
  if "balance sheet" in text or "financial position" in text:
    return "balance_sheet"
  if (
    "stockholders' equity" in text
    or "shareholders' equity" in text
    or "changes in equity" in text
  ):
    return "equity_statement"
  if (
    "comprehensive income" in text
    or "operations" in text
    or "income" in text
    or "earnings" in text
  ):
    return "income_statement"
  return None


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _normalize(value: str | None) -> str:
  # Fold curly apostrophes to ASCII so "stockholders’ equity" matches.
  return (value or "").replace("’", "'").lower()


def _normalize_uri(value: str | None) -> str:
  """Normalize a role URI's local name for phrase matching.

  Role URIs carry the statement name as a camelCase / hyphenated segment
  (``.../BalanceSheet``, ``.../StatementOfCashFlows``) rather than prose, so
  split camelCase and separators back into words before matching.
  """
  if not value:
    return ""
  local = re.split(r"[/#]", value)[-1] or value
  spaced = _CAMEL_BOUNDARY.sub(" ", local)
  spaced = re.sub(r"[-_.]+", " ", spaced)
  return spaced.replace("’", "'").lower()


# The root of a primary statement's presentation tree, by local name — the
# namespace is dropped so one entry serves IFRS, US GAAP and any filer that
# mirrors the standard name.
_ROOT_ABSTRACTS: dict[str, str] = {
  "StatementOfFinancialPositionAbstract": "balance_sheet",
  "BalanceSheetAbstract": "balance_sheet",
  "StatementOfCashFlowsAbstract": "cash_flow_statement",
  "StatementOfChangesInEquityAbstract": "equity_statement",
  "StatementOfStockholdersEquityAbstract": "equity_statement",
  "StatementOfPartnersCapitalAbstract": "equity_statement",
  "IncomeStatementAbstract": "income_statement",
  "StatementOfComprehensiveIncomeAbstract": "income_statement",
  "StatementOfIncomeAndComprehensiveIncomeAbstract": "income_statement",
}


def root_qname(arcs: Any) -> str | None:
  """The single root of a presentation tree, or ``None``.

  The root is the one concept that is a parent and never a child. A tree with
  several roots is ambiguous and classifies on its definition alone rather
  than on a guess about which root speaks for it.
  """
  parents: list[str] = []
  children: set[str] = set()
  for arc in arcs:
    parents.append(arc.from_qname)
    children.add(arc.to_qname)
  roots = {p for p in parents if p not in children}
  return roots.pop() if len(roots) == 1 else None


def _match_root(qname: str | None) -> str | None:
  """The block type a presentation root names, ignoring its namespace."""
  if not qname:
    return None
  local = qname.rsplit(":", 1)[-1]
  return _ROOT_ABSTRACTS.get(local)


def classify_network(
  role_uri: str, definition: str | None, root: str | None = None
) -> str | None:
  """Map a presentation network to a primary ``block_type`` (or ``None``).

  Returns one of ``balance_sheet`` / ``income_statement`` /
  ``cash_flow_statement`` / ``equity_statement`` for a primary statement, else
  ``None`` (the MVP skips disclosures and detail networks). Parenthetical /
  detail networks are always excluded. The definition text is matched first;
  the role URI is a fallback when it is absent or does not classify; ``root``
  — the presentation tree's root concept, from :func:`root_qname` — is the
  last resort, and the only one that reads a filing written in any language.
  """
  definition_text = _normalize(definition)
  if "parenthetical" in definition_text:
    return None
  result = _match(definition_text) if definition_text else None
  if result is not None:
    return result
  role_text = _normalize_uri(role_uri)
  if "parenthetical" in role_text:
    return None
  result = _match(role_text)
  if result is not None:
    return result
  return _match_root(root)
