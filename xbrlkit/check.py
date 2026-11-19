"""Check a report before anything trusts it: one verdict, with its reasons.

The readers are tolerant by design, because a filing is what it is. A report
about to be loaded somewhere, or one a model just wrote, needs the opposite: a
yes or a no. ``check_report`` reads a model and says whether it is whole (it has
an identity, every fact points at a concept, a period and a unit that exist),
whether it agrees with itself (duplicate facts match, every calculation foots,
the balance sheet balances), and, when asked, whether every fact says where it
came from.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .model import XbrlFact, XbrlModel

UNKNOWN_REPORT_ID = "unknown"

# Total assets against total liabilities and equity, by local name, in the
# taxonomies that name them: US GAAP (and rs-gaap, which keeps its names) and
# IFRS.
BALANCE_PAIRS: tuple[tuple[str, str], ...] = (
  ("Assets", "LiabilitiesAndStockholdersEquity"),
  ("Assets", "EquityAndLiabilities"),
)


@dataclass
class Problem:
  """One reason a report fails. ``code`` is stable; ``detail`` is for the reader."""

  code: str
  message: str
  detail: dict[str, Any] = field(default_factory=dict)

  def to_dict(self) -> dict[str, Any]:
    return {"code": self.code, "message": self.message, **self.detail}


@dataclass
class CheckResult:
  """The verdict. ``ok`` is true only with no problems."""

  problems: list[Problem] = field(default_factory=list)
  facts: int = 0
  # Totals compared against their children, and how many agreed.
  calculations_checked: int = 0
  calculations_footed: int = 0
  # ``None`` when the report carries no pair of balance-sheet totals to compare.
  balance_checked: int | None = None

  @property
  def ok(self) -> bool:
    return not self.problems

  def to_dict(self) -> dict[str, Any]:
    return {
      "ok": self.ok,
      "facts": self.facts,
      "calculations_checked": self.calculations_checked,
      "calculations_footed": self.calculations_footed,
      "balance_checked": self.balance_checked,
      "problems": [p.to_dict() for p in self.problems],
    }


def precision(decimals: str | None) -> float:
  """A fact's ``decimals`` as a number; unstated sorts below every stated one."""
  if decimals is None:
    return -math.inf
  if decimals.strip().upper() == "INF":
    return math.inf
  try:
    return float(decimals)
  except ValueError:
    return -math.inf


def tolerance(decimals: str | None) -> float:
  """Half a unit at the fact's stated precision: a total reported to the
  million foots when it is within half a million of its children."""
  stated = precision(decimals)
  if math.isinf(stated):
    return 0.5 if stated < 0 else 0.0
  return 0.5 * 10 ** (-stated)


def check_report(
  model: XbrlModel,
  *,
  expected_facts: int | None = None,
  require_source: bool = False,
) -> CheckResult:
  """Check ``model``. ``expected_facts`` is how many facts the sender says it
  sent, so one a reader dropped is caught; ``require_source`` fails any fact
  that does not say where it came from."""
  result = CheckResult(facts=len(model.facts))
  problems = result.problems

  if not model.filing.accession or model.filing.accession == UNKNOWN_REPORT_ID:
    problems.append(Problem("no_report_id", "The report has no identifier."))
  if not model.entity.cik:
    problems.append(Problem("no_entity", "The report names no entity."))
  if not model.facts:
    problems.append(Problem("no_facts", "The report carries no facts."))
  if expected_facts is not None and expected_facts != len(model.facts):
    problems.append(
      Problem(
        "fact_count",
        f"{expected_facts} facts were sent and {len(model.facts)} were read.",
        {"expected": expected_facts, "read": len(model.facts)},
      )
    )

  period_ids = {p.id for p in model.periods}
  unit_ids = {u.id for u in model.units}
  sound: list[XbrlFact] = []
  for fact in model.facts:
    concept = model.concepts.get(fact.concept_qname)
    if concept is None:
      problems.append(
        _fact_problem("undeclared_concept", "cites no declared concept", fact)
      )
    elif fact.period_id not in period_ids:
      problems.append(
        _fact_problem(
          "unknown_period", "cites a period that is not in the report", fact
        )
      )
    elif fact.unit_id is not None and fact.unit_id not in unit_ids:
      problems.append(
        _fact_problem("unknown_unit", "cites a unit that is not in the report", fact)
      )
    elif concept.is_numeric and not fact.is_nil and fact.numeric_value is None:
      problems.append(
        _fact_problem("not_a_number", "is numeric and its value is not a number", fact)
      )
    elif concept.is_numeric and not fact.is_nil and fact.unit_id is None:
      problems.append(_fact_problem("no_unit", "is numeric and has no unit", fact))
    else:
      sound.append(fact)
    if require_source and not (fact.provenance and fact.provenance.source):
      problems.append(
        _fact_problem("no_source", "does not say where it came from", fact)
      )

  best = _agree(sound, problems)
  _foot(model, best, result)
  _balance(best, result)
  return result


def _fact_problem(code: str, what: str, fact: XbrlFact) -> Problem:
  return Problem(
    code,
    f"Fact {fact.id} ({fact.concept_qname}) {what}.",
    {"fact": fact.id, "concept": fact.concept_qname},
  )


def _key(fact: XbrlFact) -> tuple[Any, ...]:
  dims = tuple(
    sorted((d.axis_qname, d.member_qname or d.typed_value or "") for d in fact.dims)
  )
  return (fact.concept_qname, fact.period_id, fact.unit_id, dims, fact.language)


def _agree(
  facts: list[XbrlFact], problems: list[Problem]
) -> dict[tuple[Any, ...], XbrlFact]:
  """One fact per coordinate, the most precise. Two facts at one coordinate
  must agree within the coarser one's precision, or the report contradicts
  itself and no reader can say which value it meant."""
  best: dict[tuple[Any, ...], XbrlFact] = {}
  reported: set[tuple[Any, ...]] = set()
  for fact in facts:
    key = _key(fact)
    current = best.get(key)
    if current is None:
      best[key] = fact
      continue
    if fact.numeric_value is not None and current.numeric_value is not None:
      allowed = max(tolerance(fact.decimals), tolerance(current.decimals))
      conflict = abs(fact.numeric_value - current.numeric_value) > allowed
    else:
      conflict = fact.value_str != current.value_str
    if conflict and key not in reported:
      reported.add(key)
      problems.append(
        Problem(
          "duplicate_conflict",
          f"{fact.concept_qname} is reported twice for one period with different values.",
          {
            "concept": fact.concept_qname,
            "values": [current.value_str, fact.value_str],
            "facts": [current.id, fact.id],
          },
        )
      )
    if precision(fact.decimals) > precision(current.decimals):
      best[key] = fact
  return best


def _totals(
  best: dict[tuple[Any, ...], XbrlFact],
) -> dict[str, dict[tuple[str, str | None], XbrlFact]]:
  """Concept → (period, unit) → its undimensioned numeric fact."""
  out: dict[str, dict[tuple[str, str | None], XbrlFact]] = defaultdict(dict)
  for fact in best.values():
    if fact.dims or fact.numeric_value is None:
      continue
    out[fact.concept_qname][(fact.period_id, fact.unit_id)] = fact
  return out


def _foot(
  model: XbrlModel, best: dict[tuple[Any, ...], XbrlFact], result: CheckResult
) -> None:
  totals = _totals(best)
  for network in model.networks:
    if network.kind != "calculation":
      continue
    children: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for arc in network.arcs:
      weight = arc.weight if arc.weight is not None else 1.0
      children[arc.from_qname].append((arc.to_qname, weight))
    for parent, terms in children.items():
      for (period_id, unit), fact in totals.get(parent, {}).items():
        computed = 0.0
        present = 0
        for child, weight in terms:
          term = totals.get(child, {}).get((period_id, unit))
          if term is None or term.numeric_value is None:
            continue
          present += 1
          computed += weight * term.numeric_value
        # A total with none of its terms reported is a total, not a sum.
        if not present or fact.numeric_value is None:
          continue
        result.calculations_checked += 1
        difference = fact.numeric_value - computed
        if abs(difference) <= tolerance(fact.decimals):
          result.calculations_footed += 1
          continue
        detail: dict[str, Any] = {
          "concept": parent,
          "role": network.role_uri,
          "period": period_id,
          "reported": fact.numeric_value,
          "computed": computed,
          "difference": difference,
        }
        if present < len(terms):
          detail["terms_missing"] = len(terms) - present
        result.problems.append(
          Problem(
            "does_not_foot",
            f"{parent} is reported as {fact.numeric_value:g} and its terms sum "
            f"to {computed:g}.",
            detail,
          )
        )


def _balance(best: dict[tuple[Any, ...], XbrlFact], result: CheckResult) -> None:
  totals = _totals(best)
  by_local: dict[str, list[str]] = defaultdict(list)
  for qname in totals:
    by_local[qname.rpartition(":")[2]].append(qname)
  for left_name, right_name in BALANCE_PAIRS:
    for left in by_local.get(left_name, ()):
      prefix = left.rpartition(":")[0]
      right = f"{prefix}:{right_name}" if prefix else right_name
      for at, assets in totals[left].items():
        other = totals.get(right, {}).get(at)
        if other is None or assets.numeric_value is None or other.numeric_value is None:
          continue
        result.balance_checked = (result.balance_checked or 0) + 1
        allowed = max(tolerance(assets.decimals), tolerance(other.decimals))
        difference = assets.numeric_value - other.numeric_value
        if abs(difference) > allowed:
          result.problems.append(
            Problem(
              "does_not_balance",
              f"{left} is {assets.numeric_value:g} and {right} is "
              f"{other.numeric_value:g}.",
              {
                "period": at[0],
                "assets": assets.numeric_value,
                "liabilities_and_equity": other.numeric_value,
                "difference": difference,
              },
            )
          )


__all__ = ("CheckResult", "Problem", "check_report", "precision", "tolerance")
