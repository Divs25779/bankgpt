"""
Turns the current state of a Playwright Page into an Observation the LLM
can reason over (src/llm/base.py). This is the concrete implementation
of "observe" in observe -> decide -> act.

WHY ACCESSIBILITY SNAPSHOT, NOT SCREENSHOT+COORDINATES (default path):
Playwright's `page.accessibility.snapshot()` (or the newer
`page.locator("body").aria_snapshot()` in recent versions) walks the
accessibility tree the browser already computes for screen readers. It
exists independent of whether the underlying markup has clean IDs,
semantic tags, or test attributes -- which is exactly the legacy-app
condition this whole project is about. A screenshot+coordinates approach
works too (and is a reasonable fallback for canvas-heavy UI with no
accessible representation at all, e.g. a canvas widget) but costs more
per call and gives you pixel coordinates as your target reference, which
are a much worse foundation for a *stable, replayable* Locator than a
role+name pair. We use screenshots only as evidence-on-failure (see
EvidenceLogger), not as the primary decision input.

TODO (build this together): implement `capture_observation` by:
  1. Calling the accessibility snapshot API on `page`.
  2. Flattening the (nested) accessibility tree into a flat list of
     ObservedElement, assigning each a stable `ref` (e.g. "e0", "e1", ...)
     for this turn -- filter to interactive/readable roles only
     (button, textbox, link, cell, heading, checkbox, combobox, dialog)
     to keep the prompt small.
  3. Wrapping into an Observation with page.url and page.title().

Keep a `ref -> Playwright locator` side table for this turn so that once
the model picks a target_ref, src/agent/loop.py can resolve it back to
something clickable -- that resolution table is also the raw material
src/artifact/compiler.py turns into stable Locators after a successful run.
"""

from __future__ import annotations

from src.llm.base import Observation


def capture_observation(page) -> tuple[Observation, dict]:
    """
    Returns (observation, ref_to_playwright_locator).

    `page` is a playwright.sync_api.Page. Left unimplemented here --
    this is one of the pieces we build together, since it's where the
    "no clean DOM" bias decision actually gets exercised against your
    chosen mock app's markup.
    """
    raise NotImplementedError("Build together: accessibility snapshot -> Observation")
