# rssFriends design language

## Product voice

**rssFriends is a social radar for the open web — not a reader.** No
unread counters, no backlog, no algorithm. Copy should:

- Say **watch** / **catch** / **signal**, not "read" / "consume" /
  "inbox". Users are scanning a horizon, not clearing a queue.
- Say **radar** / **feeds** / **friends**, not "articles" / "posts" /
  "subscribers". "Reader" is what other products are.
- Lean **open** and **portable**. RSS today, ActivityPub-shaped
  tomorrow. Every URL is subscribable, every export is OPML, every
  user can leave with their data.
- Skip guilt language. No "unread", no "you missed", no red-badge
  urgency. If a user hasn't checked in for a week, that's fine; the
  radar doesn't accumulate shame.

Tagline: **"A social radar for the open web."**

## Visual identity

The visual identity: **warm, editorial, low-chrome**. Cream page, dark
ink, burnt-orange accent, serif headlines against a clean sans body.
Owes a lot to well-designed reading products (Are.na, Substack, Craft)
without imitating them.

This doc is the source of truth for design decisions. When you touch
CSS, colors, or typography, update this file too. When it drifts from
`app/static/style.css`, the CSS wins — and the doc is broken; fix it.

Rendered previews of every page live in `/Users/…/Downloads/RSS Friends redesign/`
(HTML mockups from a Claude Design session, 2026-07-23) — treat them
as reference, not gospel; some invented UI (avatars, inline home
signup) is a separate roadmap item, not part of the design language.

## Core principles

1. **Type does the heavy lifting.** Serif headlines set the mood. Body
   copy is a clean humanist sans at generous line-height. Rely on
   size + weight + color before adding rules, boxes, or icons.
2. **Warm > neutral.** Everything is nudged 5–10° toward orange. Even
   the "gray" is a warm gray. No stark whites, no cold blacks.
3. **Big tap targets, generous whitespace.** 44×44 minimum for
   anything tappable. Sections breathe (32–64px vertical padding).
4. **Pills for CTAs, rounded rectangles for cards.** Buttons are pill-
   shaped (`radius: 99px`); cards are 14–20px radius. Nothing sharp.
5. **One accent color, used sparingly.** The burnt orange marks the
   primary CTA per screen and the "you are here" state in navigation
   or chips. If everything is accented, nothing is.
6. **Dark mode is first-class.** Every token has a dark counterpart.
   The warm feeling survives — we don't switch to a cold "night mode".

## Color palette

Tokens live in `app/static/style.css` as CSS custom properties.
Every color used in the app comes from a token; never hard-code hex
values in components.

### Light (`:root`)

| Token           | Hex       | Role                                            |
|-----------------|-----------|-------------------------------------------------|
| `--fg`          | `#221B12` | Body ink, primary text                          |
| `--muted`       | `#6F6455` | Secondary text, meta                            |
| `--bg`          | `#FBF8F3` | Page background (warm cream)                    |
| `--surface`     | `#FFFFFF` | White surface (input fields, chips)             |
| `--card`        | `#FFFFFF` | Card body                                       |
| `--card-hover`  | `#F6F0E4` | Card hover                                      |
| `--border`      | `#EDE5D6` | Hairline borders                                |
| `--accent`      | `#C75B1E` | **Primary CTA, "you are here", brand mark**     |
| `--accent-fg`   | `#FBF8F3` | Text on accent bg                               |
| `--accent-bg`   | `#F7E8DC` | Pale accent bg (BETA chip, subtle callouts)    |
| `--accent-hover`| `#A94A14` | Accent-button hover                             |
| `--link`        | `#C75B1E` | Body-text link (matches accent — one hue)       |
| `--link-hover`  | `#96430F` | Link hover                                      |
| `--danger`      | `#B3261E` | Destructive action, error text                  |
| `--star`        | `#D4A017` | Bookmark ★ fill (gold)                          |

### Dark (`[data-theme="dark"]` + `prefers-color-scheme: dark`)

| Token           | Hex       |
|-----------------|-----------|
| `--fg`          | `#F0E8D8` |
| `--muted`       | `#A69B87` |
| `--bg`          | `#1A1512` |
| `--surface`     | `#221D18` |
| `--card`        | `#26221C` |
| `--card-hover`  | `#2F2A23` |
| `--border`      | `#3A342A` |
| `--accent`      | `#E97A3E` |
| `--accent-fg`   | `#1A1512` |
| `--accent-bg`   | `#3A2818` |
| `--accent-hover`| `#F09553` |
| `--link`        | `#E97A3E` |
| `--link-hover`  | `#F09553` |

**Rules:**
- Text on `--bg` must be `--fg` or `--muted`. Never other grays.
- The accent is legal on either `--bg` or `--fg`. Nowhere else.
- Danger/star tokens are earned — reserved for their specific roles.
  Don't use `--danger` for "red text I want somewhere". If you need
  another color, add a token here first.

## Typography

Three families, all from Google Fonts. The split mirrors how Instapaper
does it — sans in the app chrome, serif when the user is *reading*.

- **`Instrument Serif`** — display headlines (h1, h2, hero titles). One
  weight (regular). Italic reserved for the *emphasized clause* pattern
  in hero copy — "Your RSS, *with friends.*" — one italic phrase max.
- **`Instrument Sans`** — the default UI font. Every button, chip, form
  field, nav item, radar list, timeline row. Weights 400/500/600/700.
- **`Source Serif 4`** — the **reading body** for prose-heavy pages
  (`/help`, hero copy, verify/onboarding text). Weights 400/500/600.
  Applied via the `.prose` utility class OR by the `.help-block`
  container. Do **not** use it for UI-shaped content (lists of items,
  chips, tables); reserve it for actual paragraphs of prose.

Fallback stacks (in case Google Fonts is blocked/slow —
`font-display: swap` prevents FOIT while loading):

- Serif display: `ui-serif, Georgia, serif`
- Sans UI:       `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`
- Serif reading: `ui-serif, Georgia, serif`

### Type scale

| Class / element      | Size    | Weight | Family | Letter-spacing |
|----------------------|---------|--------|--------|----------------|
| `h1` hero (landing)  | 64px    | 400    | Serif  | -0.015em       |
| `h1` page title      | 38–48px | 400    | Serif  | -0.015em       |
| `h2` section         | 28–36px | 400    | Serif  | -0.01em        |
| `h3` sub             | 22–26px | 400    | Serif  | -0.005em       |
| Body                 | 16px    | 400    | Sans   | 0              |
| Body large (intro)   | 17–19px | 400    | Sans   | 0              |
| UI / button          | 14–15px | 600    | Sans   | 0              |
| Meta / muted         | 13–14px | 400    | Sans   | 0              |
| Eyebrow / label      | 12–13px | 600    | Sans   | 0.10–0.12em    |
| Small pill / chip    | 11–12px | 600    | Sans   | 0.04em         |

**Line height:** 1.55 for body, 1.02–1.10 for hero, 1.4–1.5 for h2/h3.

**Text wrapping:** use `text-wrap: pretty` on body copy, `text-wrap: balance`
on headlines. Both are supported everywhere we care about.

## Spacing + layout

- **Content max-width:** `1120px`. Horizontal padding `32px` (desktop),
  `20px` (mobile, ≤640px).
- **Section rhythm:** vertical padding `72–96px` on landing sections,
  `40–56px` on content pages, `24–32px` between subsections.
- **Card padding:** `20–26px`.
- **Grid gaps:** `12–16px` between cards.
- **Component gaps:** `6px / 10px / 16px / 24px / 32px`. Stick to
  these values — no free-form `13px`.

## Radii + shadows

- Buttons + pills: `border-radius: 99px` (full pill).
- Cards: `14–20px`. Small cards `14px`, hero cards `20px`.
- Inputs: `12px`.
- Code chips: `6–8px`.

Shadows are rare and small: `box-shadow: 0 24px 48px -24px rgba(63,45,20,.18)`
on the hero preview card only. Elsewhere, borders do the work.

## Components

### Buttons

```
.btn        — accent fill, cream text, pill radius, 13-15px padding.
.btn-ghost  — 1px accent border, accent text, transparent bg.
.btn-dark   — dark ink fill (for secondary CTAs on light bg).
.btn-danger — --danger fill, --danger-fg text.
```

Hover: darken accent by ~10% (see `--accent-hover`). No transitions
longer than 120ms.

### Chips + pills

- **Filter chip** (category on radar): 1px border, muted text; when
  active, dark ink fill + cream text. `border-radius: 99px`,
  `padding: 4-7px 12-15px`, font 12-13px 500-600.
- **BETA chip**: `--accent-bg` bg + `--accent` text, `border-radius: 99px`.
- **"NEW" badge** on feed rows: no bg, small caps, `--accent` text.
- **Source-domain chip** (Friends feed): white bg, 1px border, muted text.

### Cards

Always white on cream (light) or `--card` (dark) with `1px solid --border`.
Hover state raises the card to `--card-hover`. No box-shadow except on
the hero preview.

### Nav

- Sticky top, `background: rgba(bg, .92)`, `backdrop-filter: blur(8px)`.
- Height ~60px. Brand + BETA on the left, links + sign-in on the right.
- Active link: `--accent` color, weight 600.

### Forms

Inputs: 12px radius, 13–15px font, 12–13px padding, 1px `--border`, white
bg. Focus: 1px `--accent` border (no outline glow).

## Icons

- **Brand mark**: orange rounded-square (~26×26) with three concentric
  radar arcs in cream. Used in the top nav across all pages.
- **PWA app icon**: the classic RSS glyph (orange dot + two arcs). This
  is different by design — the OS icon needs to be universally readable
  as "an RSS thing" at 40×40, while the in-app brand mark is a bespoke
  radar-dish. Both live in `app/static/icons/`.
- Everywhere else, use emoji or nothing. Never SVG icon fonts.

## Motion

Motion is a light touch:

- 120ms `ease-out` on button hover fill.
- 200ms `ease-out` on card hover bg.
- No page transitions, no scroll-jack, no parallax.

Respect `prefers-reduced-motion: reduce` — set all durations to 0 there.

## Accessibility

- **Minimum contrast**: 4.5:1 for body text on background. The current
  token pairs pass; verify with a checker before adding new ones.
- **Focus rings** are always visible on interactive elements. Never
  `outline: none` without providing a `:focus-visible` alternative.
- **Tap targets** ≥ 44×44 CSS pixels on touch. Small icons need padding
  around them, not tiny hit boxes.
- **Labels** on every form input. `aria-label` only when a visible
  label is genuinely inappropriate.

## What is NOT part of this design language

Explicitly out of scope for CSS/palette work:

- **Adding user avatars** — the mocks show circular initial avatars.
  Requires an avatar column + generator; separate feature.
- **Inline signup on the home page** — currently a `/login` route.
  Design change, not a token change.
- **Two-column radar feed grid** — the mocks show it; current radar
  is single-column. Bigger refactor of `_panel_feeds.html`.

These are on the redesign roadmap but tracked separately.

## Rollout stages

1. **Palette + fonts + type scale** — swap tokens in `style.css`, add
   font imports to `base.html`. **This is step 1** (the current step).
2. **Home page redesign** — new landing with hero + inline signup.
3. **Radar page redesign** — avatars, meta pills, two-column grid.
4. **Discover + Help polish** — already close to target structure.

## References

- Mocks (local, private): `~/Downloads/RSS Friends redesign/{Home,Radar,Discover,Help}.dc.html`
- Fonts: <https://fonts.google.com/specimen/Instrument+Sans>,
         <https://fonts.google.com/specimen/Instrument+Serif>
- Token file: `app/static/style.css` (lines under `:root` and `[data-theme="dark"]`)
