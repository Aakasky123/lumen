"""Stable control ids across a run's observations.

The browser extension and the desktop walker number an observation's controls 1..N in list order,
so the number moved whenever the list did. A body keeps one `ControlIdRegistry`; every observation's
controls are relabelled through it: a control that is the same element as one seen in a recent
observation (same selector on the same page, same tag/role/type and accessible name) keeps the id it had, and any other
control gets a fresh id that is never reused in the run. Only the label changes: list order, the raw
selector and every dispatch-time re-resolution by selector are untouched, so an id that no longer
names a listed control fails exactly as a stale per-screenshot index did.
"""

from __future__ import annotations

from typing import Any

# Identities are kept for the whole run, not for a window of observations: a form scrolled out of view
# for a long stretch (run 1824: the question fields were last seen 12+ captures before the page scrolled
# back) must return under the same ids. Only a size cap evicts, oldest-seen first; an evicted element that
# returns is new, so an id is never handed to a different element.
_MAX_REMEMBERED_CONTROLS = 800


def _accessible_name(control: dict[str, Any]) -> str:
    """The control's normalized accessible name (never its value): a selector that now names another
    row or button after a delete, re-sort or SPA swap carries a different name and so a new id.

    An element with a DOM id (unique in its document) and its own aria-label / aria-labelledby is named by
    those two, not its content text: a select button whose text is its chosen value ("30 min" -> "45 min")
    keeps its id (v6 calendar run 1904's Duration button did not). Without a DOM id the text stays in the
    name, so shared labels (every row's "Row actions") never carry an id over to another row."""
    dom_id = " ".join(str(control.get("id") or "").split())
    context = control.get("accessibility_context")
    if dom_id and isinstance(context, list):
        for relation in ("aria-labelledby", "aria-label"):
            for entry in context:
                if isinstance(entry, dict) and entry.get("relation") == relation:
                    own = " ".join(str(entry.get("text") or "").split()).casefold()
                    if own:
                        return f"#{dom_id}|{own}"[:200]
    for key in ("text", "aria_label", "label", "id", "name"):
        text = " ".join(str(control.get(key) or "").split()).casefold()
        if text:
            return text[:200]
    return ""


def _page_scope(url: Any) -> str:
    """The document a selector is meaningful in: the URL without its fragment."""
    return str(url or "").split("#", 1)[0]


class ControlIdRegistry:
    def __init__(self) -> None:
        self._next_id = 1
        self._observation = 0
        self._ids: dict[tuple[str, ...], tuple[int, int]] = {}  # key -> (id, last observation seen)
        # id -> how the element was last seen (page, selector, tag, role, type, name, label, observation)
        self._seen: dict[int, dict[str, Any]] = {}

    def relabel(self, controls: Any, *, scope: Any = "") -> dict[Any, int]:
        """Replace each control's `index` with its stable id, in place; returns old index -> stable id."""
        mapping: dict[Any, int] = {}
        if not isinstance(controls, list):
            return mapping
        self._observation += 1
        page = _page_scope(scope)
        used: set[int] = set()
        for control in controls:
            if not isinstance(control, dict):
                continue
            selector = str(control.get("selector") or "")
            key = (
                page,
                selector,
                str(control.get("tag") or ""),
                str(control.get("role") or ""),
                str(control.get("type") or ""),
                _accessible_name(control),
            )
            known = self._ids.get(key) if selector else None
            if known is not None and known[0] not in used:
                stable = known[0]
            else:
                stable = self._next_id
                self._next_id += 1
            used.add(stable)
            if selector:
                self._ids[key] = (stable, self._observation)
                self._seen[stable] = {
                    "index": stable,
                    "page": page,
                    "selector": selector,
                    "tag": key[2],
                    "role": key[3],
                    "type": key[4],
                    "name": key[5],
                    "observation": self._observation,
                }
            if control.get("index") is not None:
                mapping[control["index"]] = stable
            control["index"] = stable
        self._evict()
        return mapping

    def _evict(self) -> None:
        excess = len(self._ids) - _MAX_REMEMBERED_CONTROLS
        if excess <= 0:
            return
        oldest = sorted(self._ids.items(), key=lambda item: (item[1][1], item[1][0]))[:excess]
        dropped = {value[0] for _, value in oldest}
        self._ids = {key: value for key, value in self._ids.items() if value[0] not in dropped}
        self._seen = {stable: row for stable, row in self._seen.items() if stable not in dropped}

    def remembered(self, *, scope: Any = "", current: Any = ()) -> list[dict[str, Any]]:
        """Controls seen on this document earlier in the run but absent from the latest one (scrolled
        out of the viewport, or gone): id, selector and structural identity, nothing the page holds as a value.
        The runtime may scroll one of them back into view; it is trusted only after the next observation lists
        the same id again (which happens only for the same selector, tag, role, type and accessible name)."""
        page = _page_scope(scope)
        present = {int(item) for item in current if isinstance(item, int)}
        return [
            {key: value for key, value in row.items() if key != "observation"}
            for stable, row in sorted(self._seen.items())
            if row["page"] == page and stable not in present and row["observation"] < self._observation
        ]
