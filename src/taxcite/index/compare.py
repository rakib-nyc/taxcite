"""Comparing a provision between two points in time (roadmap 1.1).

"What did the Tax Cuts and Jobs Act actually do to § 163(j)?" is a question a tax
lawyer asks constantly and that no free tool answers. With two release points indexed
it is a diff, and the useful answer has three parts: whether the heading changed, which
subdivisions appeared or disappeared, and what the words became.
"""

from __future__ import annotations

import difflib
import sqlite3
from dataclasses import dataclass, field
from typing import Final

from taxcite.citations import normalize as norm
from taxcite.index import db
from taxcite.models import Provision, SourceType

#: How much surrounding context a unified text diff keeps.
DIFF_CONTEXT_WORDS: Final = 8


@dataclass(slots=True)
class ProvisionChange:
    """One subdivision that differs between two versions."""

    provision_id: str
    display: str
    kind: str  # "added" | "removed" | "changed" | "renamed"
    before_heading: str | None = None
    after_heading: str | None = None
    diff: str | None = None


@dataclass(slots=True)
class Comparison:
    """The difference in one provision between two release points."""

    citation: str
    before_version: str
    after_version: str
    existed_before: bool = True
    exists_after: bool = True
    changes: list[ProvisionChange] = field(default_factory=list)

    @property
    def unchanged(self) -> bool:
        """Return ``True`` if nothing differs."""
        return self.existed_before and self.exists_after and not self.changes


def _subtree(connection: sqlite3.Connection, root_id: str) -> dict[str, Provision]:
    """Return a provision and every descendant, keyed by canonical id."""
    root = db.get_provision(connection, root_id)
    if root is None:
        return {}
    found = {root.id: root}
    for child in db.get_descendants(connection, root_id):
        found[child.id] = child
    return found


def _word_diff(before: str, after: str) -> str | None:
    """Render a compact word-level diff, or ``None`` if the text is identical."""
    before_words = before.split()
    after_words = after.split()
    if before_words == after_words:
        return None
    matcher = difflib.SequenceMatcher(a=before_words, b=after_words, autojunk=False)
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            span = before_words[i1:i2]
            if len(span) <= DIFF_CONTEXT_WORDS * 2:
                parts.append(" ".join(span))
            else:
                head = " ".join(span[:DIFF_CONTEXT_WORDS])
                tail = " ".join(span[-DIFF_CONTEXT_WORDS:])
                parts.append(f"{head} … {tail}")
            continue
        if i1 != i2:
            parts.append(f"~~{' '.join(before_words[i1:i2])}~~")
        if j1 != j2:
            parts.append(f"**{' '.join(after_words[j1:j2])}**")
    return " ".join(part for part in parts if part)


def compare_provision(
    before: sqlite3.Connection,
    after: sqlite3.Connection,
    provision_id: str,
    *,
    citation: str | None = None,
) -> Comparison:
    """Compare one provision and its subdivisions between two indexes."""
    result = Comparison(
        citation=citation or provision_id,
        before_version=db.get_meta(before, "usc_release_point") or "unknown",
        after_version=db.get_meta(after, "usc_release_point") or "unknown",
    )
    old = _subtree(before, provision_id)
    new = _subtree(after, provision_id)
    result.existed_before = bool(old)
    result.exists_after = bool(new)
    if not old or not new:
        return result

    for identifier in sorted(set(old) | set(new), key=_sort_key):
        old_node = old.get(identifier)
        new_node = new.get(identifier)
        display = _display(new_node or old_node)
        if old_node is None and new_node is not None:
            result.changes.append(
                ProvisionChange(identifier, display, "added", after_heading=new_node.heading)
            )
            continue
        if new_node is None and old_node is not None:
            result.changes.append(
                ProvisionChange(identifier, display, "removed", before_heading=old_node.heading)
            )
            continue
        if old_node is None or new_node is None:  # pragma: no cover - covered above
            continue
        text_diff = _word_diff(old_node.text, new_node.text)
        heading_changed = (old_node.heading or "") != (new_node.heading or "")
        if not text_diff and not heading_changed:
            continue
        result.changes.append(
            ProvisionChange(
                identifier,
                display,
                "renamed" if heading_changed and not text_diff else "changed",
                before_heading=old_node.heading,
                after_heading=new_node.heading,
                diff=text_diff,
            )
        )
    return result


def _display(provision: Provision | None) -> str:
    """Render a provision's display citation."""
    if provision is None:  # pragma: no cover - callers always pass one
        return ""
    return norm.display_for(provision.source, provision.section, provision.path)


def _sort_key(identifier: str) -> tuple[int, str]:
    """Sort provisions shallowest first, then lexically, so a diff reads top-down."""
    return (identifier.count("/"), identifier)


def to_markdown(comparison: Comparison) -> str:
    """Render a comparison for a human."""
    out = [
        f"# {comparison.citation}: {comparison.before_version} → {comparison.after_version}",
        "",
    ]
    if not comparison.existed_before:
        out += [f"Did not exist at release point {comparison.before_version}.", ""]
        return "\n".join(out)
    if not comparison.exists_after:
        out += [f"No longer exists at release point {comparison.after_version}.", ""]
        return "\n".join(out)
    if comparison.unchanged:
        out += ["No change.", ""]
        return "\n".join(out)

    counts: dict[str, int] = {}
    for change in comparison.changes:
        counts[change.kind] = counts.get(change.kind, 0) + 1
    out += [" · ".join(f"{count} {kind}" for kind, count in sorted(counts.items())), ""]

    for change in comparison.changes:
        out.append(f"## {change.display} — {change.kind}")
        if change.kind == "added":
            out.append(f"- **New:** {change.after_heading or '(no heading)'}")
        elif change.kind == "removed":
            out.append(f"- **Was:** {change.before_heading or '(no heading)'}")
        else:
            if (change.before_heading or "") != (change.after_heading or ""):
                out.append(
                    f"- **Heading:** ~~{change.before_heading or '(none)'}~~ "
                    f"→ **{change.after_heading or '(none)'}**"
                )
            if change.diff:
                out.append(f"- **Text:** {change.diff}")
        out.append("")
    return "\n".join(out)


def section_inventory(connection: sqlite3.Connection) -> set[str]:
    """Return every IRC section number in an index, for whole-corpus comparison."""
    return set(db.section_numbers(connection, SourceType.IRC))


def compare_corpora(
    before: sqlite3.Connection, after: sqlite3.Connection
) -> tuple[list[str], list[str]]:
    """Return the sections added and removed between two release points."""
    old = section_inventory(before)
    new = section_inventory(after)
    return sorted(new - old, key=_section_key), sorted(old - new, key=_section_key)


def _section_key(section: str) -> tuple[int, str]:
    """Sort section numbers numerically where possible."""
    digits = "".join(ch for ch in section if ch.isdigit())
    return (int(digits) if digits else 0, section)
