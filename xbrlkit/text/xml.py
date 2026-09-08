"""SEC XML documents — the forms that carry no XBRL.

By count, most of EDGAR is neither inline nor classic XBRL: it is XML. The
ownership forms alone (3, 4, 5) outnumber every XBRL filing a company ever
makes — for one large filer they are 85% of the submission history — and
13F-HR, N-PORT, N-CEN, SC 13D/G and Form 144 are XML too. None of them has an
instance, a taxonomy or a presentation network, so nothing in the XBRL side of
this library reaches them, and rendering them as prose would throw away the
structure that is the whole point of the form.

They do not need per-form parsers. Every one of these schemas is shallow,
regular, and built the same way: scalar header fields, then repeated groups of
identical elements — the transactions on a Form 4, the holdings in a 13F. So
this reads *any* of them into the same two things, a table of fields and a set
of record tables, without knowing which form it is looking at.

Two shapes recur across the SEC's schemas and both are handled here:

* a ``value`` wrapper — ``<transactionShares><value>65000</value></...>`` is
  one number, not a nested object, and collapses to one field;
* a ``*Table`` container — ``<nonDerivativeTable>`` holds transactions even
  when there is exactly one, so its children are records however many there
  are, while elsewhere a group must actually repeat to become one.

Attributes are content in these schemas, not markup: a footnote carries its
text with an ``id`` attribute and is referenced by ``<footnoteId id="F1"/>``,
an element with no text at all. Both are kept, an attribute under ``path@name``,
so a price marked as footnoted still says which footnote explains it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree import ElementTree

# EDGAR serves ownership forms twice: `xslF345X06/name.xml` is the form run
# through the SEC's own XSL and comes back as HTML, while the same name without
# that prefix is the XML the filer submitted. `primaryDocument` names the
# rendered one, so the machine-readable document is the prefix stripped off.
_XSL_PREFIX = re.compile(r"^xsl[A-Za-z0-9_]*/")


def raw_document_name(primary_document: str) -> str:
  """The submitted document behind EDGAR's rendered path for it."""
  return _XSL_PREFIX.sub("", primary_document or "")


def is_rendered_path(primary_document: str) -> bool:
  """Whether EDGAR names a rendered view rather than the submitted file."""
  return bool(_XSL_PREFIX.match(primary_document or ""))


@dataclass
class XmlTable:
  """One repeated group: the records under a name, and their columns."""

  name: str
  columns: list[str]
  rows: list[dict[str, str]] = field(default_factory=list)


@dataclass
class XmlDocument:
  """One SEC XML document read as fields and record tables."""

  root: str
  fields: dict[str, str] = field(default_factory=dict)
  tables: list[XmlTable] = field(default_factory=list)

  @property
  def form_hint(self) -> str | None:
    """The form the document calls itself, when it says so."""
    for key in ("documentType", "submissionType", "type"):
      for path, value in self.fields.items():
        if path == key or path.endswith("." + key):
          return value
      _ = key
    return None


def parse_xml_document(source: str | bytes) -> XmlDocument:
  """Read a SEC XML document into fields and record tables.

  Raises :class:`xml.etree.ElementTree.ParseError` when it is not XML.
  """
  root = ElementTree.fromstring(source)
  doc = XmlDocument(root=_tag(root))
  _walk(root, "", doc)
  return doc


def render(doc: XmlDocument) -> str:
  """The document as readable text: its fields, then each table as markdown.

  This is what ``search_text`` searches and ``read_text`` pages, so every
  value in the document has to appear in it — a name, a date, a share count.
  """
  lines = [f"# {doc.root}", ""]
  for path, value in doc.fields.items():
    lines.append(f"{path}: {value}")
  for table in doc.tables:
    lines += ["", f"## {table.name} ({len(table.rows)} rows)", ""]
    lines.append("| " + " | ".join(table.columns) + " |")
    lines.append("| " + " | ".join("---" for _ in table.columns) + " |")
    for row in table.rows:
      lines.append("| " + " | ".join(row.get(c, "") for c in table.columns) + " |")
  return "\n".join(lines).strip()


def _tag(element: ElementTree.Element) -> str:
  """The local name, without the namespace some SEC schemas carry."""
  tag = element.tag
  return tag.rsplit("}", 1)[-1] if isinstance(tag, str) and "}" in tag else str(tag)


def _text(element: ElementTree.Element) -> str:
  return (element.text or "").strip()


def _scalar(element: ElementTree.Element) -> str | None:
  """The element's value when it is one: its own text, or a ``value`` wrapper."""
  children = list(element)
  if not children:
    return _text(element)
  named = [c for c in children if _tag(c) == "value"]
  if named and all(not list(c) for c in children):
    return _text(named[0])
  return None


def _is_records(parent: ElementTree.Element, group: list[ElementTree.Element]) -> bool:
  """Whether a group of same-named siblings is a set of records.

  Repeating is enough on its own. So is a container that says its child is a
  record even when there is one of them — ``<nonDerivativeTable>`` around a
  transaction, ``<footnotes>`` around a footnote — so a form reporting one row
  and a form reporting six read the same way.
  """
  if len(group) > 1:
    return True
  parent_tag = _tag(parent)
  name = _tag(group[0])
  # A lone record still sits in a container that says so: `<nonDerivativeTable>`
  # holding one transaction, `<footnotes>` holding one footnote. Both read as a
  # table of one, so a form with one row and a form with six read alike.
  return parent_tag.endswith(("Table", "Tables")) or parent_tag in (
    f"{name}s",
    f"{name}es",
  )


def _walk(element: ElementTree.Element, prefix: str, doc: XmlDocument) -> None:
  groups: dict[str, list[ElementTree.Element]] = {}
  for child in element:
    groups.setdefault(_tag(child), []).append(child)
  for name, group in groups.items():
    if _is_records(element, group):
      rows = [_flatten(member) for member in group]
      columns: list[str] = []
      for row in rows:
        for key in row:
          if key not in columns:
            columns.append(key)
      doc.tables.append(XmlTable(name=name, columns=columns, rows=rows))
      continue
    child = group[0]
    path = f"{prefix}{name}"
    _attributes(child, path, doc.fields)
    value = _scalar(child)
    if value is not None:
      if value:
        doc.fields[path] = value
      continue
    _walk(child, f"{path}.", doc)


def _flatten(element: ElementTree.Element) -> dict[str, str]:
  """One record's leaves as a flat map of dotted path to value.

  A record can be a scalar itself — a footnote is text under an ``id`` — so
  its own text and attributes are columns too.
  """
  out: dict[str, str] = {}
  _attributes(element, "", out)
  own = _text(element)
  if own and not list(element):
    out["text"] = own

  def visit(node: ElementTree.Element, prefix: str) -> None:
    for child in node:
      name = _tag(child)
      path = f"{prefix}{name}"
      _attributes(child, path, out)
      value = _scalar(child)
      if value is not None:
        if value:
          out[path] = value
        # A `value` wrapper can have siblings that are pure markers — a
        # `<footnoteId id="F1"/>` saying this number is explained elsewhere.
        # Collapsing the wrapper must not lose them.
        for marker in child:
          _attributes(marker, f"{path}.{_tag(marker)}", out)
        continue
      visit(child, f"{path}.")

  visit(element, "")
  return out


def _attributes(element: ElementTree.Element, path: str, out: dict[str, str]) -> None:
  """Record an element's attributes under ``path@name``."""
  for name, value in element.attrib.items():
    local = name.rsplit("}", 1)[-1]
    if value:
      out[f"{path}@{local}" if path else f"@{local}"] = value


__all__ = [
  "XmlDocument",
  "XmlTable",
  "is_rendered_path",
  "parse_xml_document",
  "raw_document_name",
  "render",
]
