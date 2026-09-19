"""
Turns the current state of a Playwright Page into an Observation the LLM
can reason over (src/llm/base.py). This is the concrete implementation
of "observe" in observe -> decide -> act.

WHY page.locator("body").aria_snapshot() (YAML), NOT page.accessibility.snapshot():
The latter is deprecated by Playwright. aria_snapshot() has been stable
since v1.49 and is the currently-recommended way to read the
accessibility tree.

WHY NOT locator.ariaSnapshotJSON() (JSON instead of YAML)? It exists and
would be a nicer shape to parse -- but it shipped in Playwright v1.63.0
(~Sept 2026), about two weeks before this was written, and this repo is
pinned to an older, one-year-stable API on purpose. The one requirement
in this entire project that cannot be flaky is the discovery run itself
("the discovery run has to be real" -- it's the one thing that can't be
mocked). Building that on a two-week-old API neither of us could fully
verify was a deliberate no: boring-and-proven beats new-and-nicer for
the single path this project cannot afford to have break. See
REPORT.md section 1 for this trade-off written up properly.

WHY OUR OWN ref SCHEME, NOT PLAYWRIGHT'S NATIVE aria-ref MECHANISM
(the one Playwright's own MCP / AI tooling uses internally)? Two
independent reasons:
  1. That mechanism is Playwright-MCP-internal plumbing, not a stably
     documented third-party contract -- risk we don't need to take on.
  2. Playwright's own docs are explicit that those refs are valid only
     within one snapshot and go stale the moment the page changes. That
     makes them fine for the LLM's *immediate* next click during
     discovery, but useless for something a CapabilityArtifact needs to
     reference months later during replay -- there is no snapshot to
     resolve them against anymore.
  Our own ref (e0, e1, ...) gets the same ergonomic win -- the model
  picks an opaque id instead of inventing a CSS selector -- without
  taking on either problem: it's resolved via the fully public,
  standard `page.get_by_role(role, name=...)` API, and the compiler
  (src/artifact/compiler.py) converts (ref -> role/name captured *at
  that turn*) into a durable Locator the model never has to reason
  about at all.

IMPLEMENTATION NOTES (resolved during review -- see REPORT.md section 7
for the TARGET_ROLES trade-off specifically):
  - TARGET_ROLES excludes `cell` and `heading`. In the mock app's
    table-based layout these are structural containers that flooded the
    observation with noise (duplicate/concatenated text from every
    wrapping table cell) while the actual interactive elements
    (button, textbox, link) were already captured cleanly on their own.
  - An element with no quoted accessible name (either genuinely absent,
    or a regex-pattern name like /foo/ in the aria snapshot) is SKIPPED
    entirely rather than coerced to an empty-string name. Coercing to ""
    and calling get_by_role(name="") is falsy in Python, so it silently
    passes name=None to Playwright -- which doesn't mean "match unnamed
    elements", it means "don't filter by name at all", matching *every*
    element of that role on the page. `.nth(count)` would then resolve
    to whichever one happens to be first in DOM order: a silent
    mis-mapping, not a crash. Known gap: an icon-only control with no
    accessible name is simply unreachable via this perception strategy
    right now.
  - get_by_role(..., exact=True): Playwright's default name matching is
    substring and case-insensitive, which would make "Search" also
    match a hypothetical "Search Filters" button -- the opposite of the
    stable, unambiguous targeting the schema's role_name strategy is
    supposed to guarantee. The replay executor MUST reconstruct this
    same call with exact=True, or a saved artifact could resolve to a
    different element during replay than the one discovery recorded.
  - LABELS (non-interactive readable data): any leaf whose text ends in
    ":" (e.g. "Savings Balance:") is collected into Observation.labels,
    independent of the TARGET_ROLES filter above -- this runs for every
    leaf regardless of role, so a labelled `cell` still contributes a
    label even though `cell` itself is excluded from `elements`. Labels
    carry no `ref`: the model reads one by name (ActionKind.READ_TEXT
    with text_value=<label>, no target_ref), not by pointing at
    something. This is deliberately read-only and value-less -- the
    model sees "Savings Balance" exists and is readable, never today's
    actual balance; see src/artifact/compiler.py's LABEL_SIBLING
    handling for how a chosen label becomes a durable Locator, and
    REPORT.md section 7 for the adjacency-only scope of this approach
    (label and value must be DOM-adjacent siblings, e.g. two cells in
    the same table row -- true for this mock app, not universal).
"""

from __future__ import annotations

from src.llm.base import Observation, ObservedElement

import re
import yaml

TARGET_ROLES = {"button", "textbox", "link", "checkbox", "combobox", "dialog"}
# Matches e.g. 'button "Search" [level=2]' -> group(1)="button", group(2)="Search"
LEAF_PATTERN = re.compile(r'^(\w+)(?:\s+"([^"]*)")?')


def capture_observation(page) -> tuple[Observation, dict]:
    """
    Returns (observation, ref_to_playwright_locator).
    """
    yaml_text = page.locator("body").aria_snapshot()
    parsed = yaml.safe_load(yaml_text) if yaml_text else None

    elements = []
    extracted_labels = []
    ref_to_locator = {}
    seen_counts = {}

    def process_leaf(leaf_str: str):
        match = LEAF_PATTERN.match(leaf_str)
        if not match:
            return

        role = match.group(1)
        name = match.group(2)

        if name and name.strip().endswith(":"):
            if name not in extracted_labels:
                extracted_labels.append(name)

        if role not in TARGET_ROLES:
            return
        if name is None:
            # No quoted accessible name -- skip rather than risk a
            # silent mis-mapping via get_by_role(name=None) matching
            # every element of this role. See module docstring.
            return

        key = (role, name)
        count = seen_counts.get(key, 0)
        seen_counts[key] = count + 1

        ref = f"e{len(elements)}"
        elements.append(ObservedElement(ref=ref, role=role, name=name))
        ref_to_locator[ref] = page.get_by_role(role, name=name, exact=True).nth(count)

    def traverse(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str):
                    process_leaf(k)
                traverse(v)
        elif isinstance(node, list):
            for item in node:
                traverse(item)
        elif isinstance(node, str):
            process_leaf(node)

    if parsed:
        traverse(parsed)

    obs = Observation(
        url=page.url,
        title=page.title(),
        elements=elements,
        labels=extracted_labels
    )
    return obs, ref_to_locator
