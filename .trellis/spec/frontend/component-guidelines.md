# Component Guidelines

> How components are built in this project.

---

## Overview

<!--
Document your project's component conventions here.

Questions to answer:
- What component patterns do you use?
- How are props defined?
- How do you handle composition?
- What accessibility standards apply?
-->

(To be filled by the team)

---

## Component Structure

<!-- Standard structure of a component file -->

(To be filled by the team)

---

## Props Conventions

<!-- How props should be defined and typed -->

(To be filled by the team)

---

## Styling Patterns

<!-- How styles are applied (CSS modules, styled-components, Tailwind, etc.) -->

(To be filled by the team)

---

## Accessibility

<!-- A11y requirements and patterns -->

(To be filled by the team)

---

## Common Mistakes

<!-- Component-related mistakes your team has made -->

### Console V2 dropdowns (2026-09-30, feat/v2-select)

- **Never render a native `<select>` in `frontend/src/v2/**`.** Form fields use
  `Select` (`v2/ui.tsx`), toolbar/chip triggers use `FilterSelect`; both share the
  `useListbox` popup (keyboard, a11y, search box auto-on above 8 options).
  `<datalist>` suggestions for free-text inputs are fine — they are not dropdowns.
- Options are `{ value, label, disabled?, group? }` with **string** values: numeric
  selects pass `String(n)` and convert back with `Number(v)` in `onChange`. A leading
  `<option value="">` becomes `placeholder` (still a selectable "" option);
  `<optgroup>` becomes `group` on consecutive options.
- The popup is `position: fixed` **inside** the `.v2` tree (the `--v2-*` tokens live
  on `.v2`, so no `document.body` portal). Do not add `transform`/`filter`/`contain`
  to V2 containers — it would re-anchor fixed popups and clip them.
- Esc in an open list calls `stopPropagation()` so an enclosing Modal/Drawer
  (`window` keydown listener) stays open; the popup root `preventDefault`s clicks so a
  wrapping `<label>` cannot re-activate the trigger and reopen the list.
