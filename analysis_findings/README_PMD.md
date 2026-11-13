# Threshold Bar — Power BI Measure

A DAX measure that draws a small picture (an SVG image) for each row of a Power BI table:

- **Standard metrics** get a fixed-width bar split into four colored bands (Unstable / Monitor / Stable / Optimized), with the threshold numbers under the band boundaries and a tick marking the metric's current value.
- **Binary metrics** (`Binary = 1`) get a pass/fail icon instead: a green check for `Binary_value = 1`, a red X for `Binary_value = 0`.

Because every row is just an image, bars and icons live in the same table column, every bar is the same width, and the table scrolls natively when there are many metrics.

---

## Contents

1. [Requirements and setup](#1-requirements-and-setup)
2. [How the code works](#2-how-the-code-works)
3. [Change: make everything bigger or smaller](#3-change-make-everything-bigger-or-smaller)
4. [Change: use a different color scheme](#4-change-use-a-different-color-scheme)
5. [Change: smart decimal places for threshold labels](#5-change-smart-decimal-places-for-threshold-labels)
6. [Troubleshooting](#6-troubleshooting)
7. [Reference: full measure](#7-reference-full-measure)

---

## 1. Requirements and setup

### Expected columns

The measure assumes a table named `Metrics` with one row per metric. Rename these references in the code if your names differ.

| Column | Type | Meaning |
|---|---|---|
| `Value` | Decimal | The metric's current value |
| `Optimized` | Decimal | Threshold for the best band (blue) |
| `Stable` | Decimal | Threshold for the second band (green) |
| `Monitor` | Decimal | Threshold for the third band (yellow); beyond it is Unstable (red) |
| `Target Direction` | Text | `">"` = higher is better, `"<"` = lower is better |
| `Binary` | Whole number | `1` = pass/fail metric, `0` = standard metric |
| `Binary_value` | Whole number | For binary metrics: `1` = pass, `0` = fail |
| `Full Weight` | Decimal | Displayed in its own column; grayed out for binary rows |

**Threshold order must match the direction**, or the bands will overlap:

- `>` rows: `Monitor < Stable < Optimized`
- `<` rows: `Optimized < Stable < Monitor`

Binary rows do not need threshold values.

> If `Binary` or `Binary_value` is stored as text, change `= 1` to `= "1"` wherever it appears.

### Setup in Power BI Desktop

1. Create the `Threshold Bar` measure (full code in [section 7](#7-reference-full-measure)).
2. Click the measure **in the Fields pane** (not in a visual). On the **Measure tools** tab, set **Data category** to **Image URL**. Without this step the table shows a long text string instead of a picture.
3. Add a **Table** visual with the metric name, `Full Weight`, and `Threshold Bar`.
4. In the table's format pane, under **Image size**, set **Width = 328** and **Height = 46**.

### Graying out Full Weight on binary rows

A separate measure supplies the font color:

```dax
Full Weight Font Color =
IF ( SELECTEDVALUE ( Metrics[Binary] ) = 1, "#A6A6A6", "#252423" )
```

Apply it under **Format → Cell elements →** series **Full Weight → Font color → fx → Format style: Field value**, and pick this measure. For a stronger effect, make a second measure returning `"#F2F2F2"` for binary rows (and `BLANK()` otherwise) and apply it to **Background color** the same way.

---

## 2. How the code works

### The big idea

The measure builds a text string that is a tiny SVG image, prefixed with `data:image/svg+xml;utf8,`. Power BI treats that string as an image URL and draws it. Nothing is downloaded from the internet. The whole picture lives inside the text, which also means it works on locked-down networks.

### SVG in 60 seconds

SVG is a text format for drawings. This measure uses only four kinds of shapes:

| Element | What it draws | Key attributes |
|---|---|---|
| `<rect>` | A rectangle (each color band) | `x`, `y` = top-left corner; `width`, `height`; `fill` = color |
| `<line>` | A straight line (the value tick) | `x1,y1` → `x2,y2`; `stroke` = color; `stroke-width` |
| `<text>` | A label | `x`, `y` = anchor point; `text-anchor='middle'` centers it on `x` |
| `<circle>` / `<path>` | The pass/fail icon | `cx, cy, r` for the circle; `d` = drawing commands for the check or X |

**Coordinates are in pixels, measured from the top-left corner.** `x` grows to the right and `y` grows *downward*, so a larger `y` is lower in the picture.

### Why `%23` instead of `#`

Inside a URL, `#` has a special meaning and would cut the image off. So every color inside the SVG writes `#` as its URL-encoded form, `%23`. `%23D64545` means `#D64545`. (The `Full Weight Font Color` measure uses a normal `#`, because it's ordinary conditional formatting and not part of a URL.)

### Walking through the measure, top to bottom

DAX evaluates the `VAR` lines lazily: a variable is only calculated if the final result actually uses it. That's why binary rows with blank thresholds don't cause errors. The bar variables are never calculated for them.

| Section | Variables | What it does |
|---|---|---|
| **Inputs** | `_val`, `_opt`, `_stb`, `_mon`, `_isHigh`, `_isBinary`, `_binVal` | Reads the current row's columns. `SELECTEDVALUE` returns the value when the table row is a single metric. |
| **Colors** | `_red`, `_yellow`, `_green`, `_blue` | The palette, shared by the bar and the icon. |
| **Binary icon** | `_pass`, `_iconColor`, `_iconMark`, `_iconSvg` | Draws a colored circle, then a white check (pass) or X (fail) on top, then the word "Pass" or "Fail". |
| **Scale** | `_min`, `_max`, `_pad`, `_lo`, `_span` | Finds the lowest and highest of the value and the three thresholds, then pads 15% on each side. This range is mapped onto the bar's full width, so every bar is the same length whatever its numbers. If all four numbers are equal, it pads by 1 to avoid dividing by zero. |
| **Layout** | `_left`, `_barW`, `_right`, `_barY`, `_barH`, `_lblY` | Where the bar sits inside the 328 × 46 image: a 14 px margin on the left, a 300 px wide bar, 16 px from the top, 14 px tall, with labels 11 px below the bar. |
| **Positions** | `_xOpt`, `_xStb`, `_xMon`, `_xVal` | Converts each number into an x-pixel position: `left + (number − lo) / span × barWidth`. |
| **Boundaries** | `_b1`–`_b3`, `_t1`–`_t3` | Puts the three threshold positions in left-to-right order. For `>` metrics the order is Monitor, Stable, Optimized; for `<` metrics it's Optimized, Stable, Monitor. `_t1`–`_t3` are the matching numbers for the labels. |
| **Segment colors** | `_c1`–`_c4` | The four band colors in left-to-right order. `>` runs red → yellow → green → blue; `<` runs blue → green → yellow → red. |
| **Drawing** | `_rects`, `_labels`, `_marker` | Builds the four `<rect>` bands, the three threshold labels, and the value tick with its label above the bar. |
| **Assembly** | `_barSvg` | Wraps the pieces in an `<svg>` tag with the data-URI prefix. |
| **Output** | `RETURN SWITCH(...)` | Chooses what to show, checking in order: binary with no result → blank; binary → icon; no value → blank; otherwise → bar. |

### The image layout at a glance

```
y = 0   ┌───────────────────────────────────────────────┐
        │                      0.87   ← value label (y=11)
y = 16  │  ███████████|████████|██████|█████████  ← bar (16 → 30)
y = 30  │                     ^ value tick (13 → 33)
y = 41  │          0.70     0.80    0.95   ← threshold labels
y = 46  └───────────────────────────────────────────────┘
        x=0  x=14 (bar start)              x=314 (bar end)  x=328
```

---

## 3. Change: make everything bigger or smaller

### The approach

Add a `viewBox` to the SVG and a single `_scale` variable. A `viewBox` tells the image "my drawing coordinates run from 0 to 328 across and 0 to 46 down." The image can then be displayed at any size and everything (bar, text, tick, icon) scales together without being clipped or distorted. You never touch the layout numbers.

### Steps

**1. Add a scale variable** near the top of the measure, right after the color variables. It must come before `_iconSvg` because both images use it.

```dax
-- Size: 1 = original (328 × 46). 1.5 = 50% bigger, 0.8 = 20% smaller.
VAR _scale = 1.5
```

**2. Replace the opening `<svg ...>` tag in `_iconSvg`** with:

```dax
"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 328 46' width='" & 328 * _scale & "' height='" & 46 * _scale & "' font-family='Segoe UI' font-size='11' fill='%23333333'>" &
```

**3. Replace the opening `<svg ...>` tag in `_barSvg`** with:

```dax
"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 328 46' width='" & 328 * _scale & "' height='" & 46 * _scale & "' font-family='Segoe UI' font-size='10' fill='%23333333'>" &
```

Only `viewBox` and the `width` / `height` values change. The font sizes stay as they are, because the `viewBox` scales them along with everything else.

**4. Update the table's Image size** to match: width = `328 × scale`, height = `46 × scale`. Power BI uses this setting to decide how big each image is displayed, so it needs to agree with `_scale`.

| `_scale` | Image width | Image height |
|---|---|---|
| 0.8 | 262 | 37 |
| 1.0 | 328 | 46 |
| 1.25 | 410 | 58 |
| 1.5 | 492 | 69 |
| 2.0 | 656 | 92 |

> Keep the width-to-height ratio at roughly 7.1 : 1. If the table's image size has a different ratio from the SVG, the picture is fitted inside the cell with empty space on one side instead of being stretched.

### Changing proportions instead (optional)

To change the *shape* rather than the overall size (for example a thicker bar or wider gaps), edit the **Layout** variables instead:

- `_barH`: bar thickness. If you increase it, increase the `46` canvas height (in the `viewBox`, `height`, and the table's image size) by the same amount so the threshold labels still fit.
- `_barW`: bar length. If you change it, the canvas width becomes `_barW + 28` (14 px margin on each side). Update `328` everywhere it appears.
- The icon's positions (`cx='23'`, `cy='23'`, the path coordinates, the text at `x='38'`) are fixed numbers. They stay vertically centered as long as the canvas height stays 46.

---

## 4. Change: use a different color scheme

### Where the colors live

| What | Where in the code | Current value |
|---|---|---|
| Unstable band, Fail icon | `_red` | `%23D64545` |
| Monitor band | `_yellow` | `%23F2C94C` |
| Stable band, Pass icon | `_green` | `%2327AE60` |
| Optimized band | `_blue` | `%232F80ED` |
| All text (labels, value, Pass/Fail) | `fill='%23333333'` in both `<svg>` tags | dark gray |
| Value tick | `stroke='%23222222'` in `_marker` | near-black |
| Check / X mark | `stroke='white'` in `_iconMark` | white |
| Full Weight, grayed | `Full Weight Font Color` measure | `#A6A6A6` (plain `#`) |

### Steps

1. Pick your hex colors (for example from your agency's style guide or the report theme).
2. Replace the `#` with `%23` and paste them into the four color variables. `#005EA2` becomes `%23005EA2`.
3. If the new colors are dark, consider whether the threshold labels and value tick still stand out. Change the text `fill` or the tick `stroke` if needed.

**Shortcut:** if you'd rather not encode `#`, SVG also accepts `rgb()` values, which need no encoding:

```dax
VAR _red = "rgb(214,69,69)"
```

### Ready-made palettes

Copy any block over the existing four color variables.

**Default (current)**

```dax
VAR _red    = "%23D64545"
VAR _yellow = "%23F2C94C"
VAR _green  = "%2327AE60"
VAR _blue   = "%232F80ED"
```

**Colorblind-safe (Okabe–Ito)**: distinguishable for the most common types of color vision deficiency.

```dax
VAR _red    = "%23D55E00"
VAR _yellow = "%23F0E442"
VAR _green  = "%23009E73"
VAR _blue   = "%230072B2"
```

**Muted**: softer tones for dense tables.

```dax
VAR _red    = "%23E07B7B"
VAR _yellow = "%23F5D98B"
VAR _green  = "%237BC39A"
VAR _blue   = "%237BA7E0"
```

**U.S. Web Design System**: the federal design standard's state colors.

```dax
VAR _red    = "%23D54309"
VAR _yellow = "%23FFBE2E"
VAR _green  = "%2300A91C"
VAR _blue   = "%23005EA2"
```

> DAX can't read the report's theme, so these colors don't update automatically when the theme changes. Change them here whenever the theme changes.

---

## 5. Change: smart decimal places for threshold labels

### Goal

Show threshold labels with only as many decimal places as they need:

| Thresholds in the row | Displayed as |
|---|---|
| 1.00, 2.00, 3.00 | 1, 2, 3 |
| 0.50, 1.00, 1.50 | 0.5, 1.0, 1.5 |
| 0.25, 0.50, 0.75 | 0.25, 0.50, 0.75 |

All three labels on a bar always share the same number of decimals, so they line up and read consistently.

### How it works

For each threshold, compare it rounded to 0 decimals (and to 1 decimal) against its value at 2 decimals. If they match for **all three** thresholds, those extra digits are all zeros and can be dropped. Comparing rounded values, rather than testing for exact equality, avoids tiny floating-point errors (like `0.5` being stored as `0.50000000001`).

### Steps

**1. Add these variables** right after the **Boundaries** section (after `_t3` is defined):

```dax
-- Threshold label precision: fewest decimals (0, 1 or 2) that lose nothing
VAR _allWhole =
    ROUND ( _t1, 0 ) = ROUND ( _t1, 2 )
        && ROUND ( _t2, 0 ) = ROUND ( _t2, 2 )
        && ROUND ( _t3, 0 ) = ROUND ( _t3, 2 )
VAR _allTenths =
    ROUND ( _t1, 1 ) = ROUND ( _t1, 2 )
        && ROUND ( _t2, 1 ) = ROUND ( _t2, 2 )
        && ROUND ( _t3, 1 ) = ROUND ( _t3, 2 )
VAR _tFmt =
    IF ( _allWhole, "0", IF ( _allTenths, "0.0", "0.00" ) )
```

**2. In `_labels`**, replace the three occurrences of `FORMAT ( _tN, "0.00" )` with `FORMAT ( _tN, _tFmt )`:

```dax
VAR _labels =
    "<text x='" & _b1 & "' y='" & _lblY & "' text-anchor='middle'>" & FORMAT ( _t1, _tFmt ) & "</text>" &
    "<text x='" & _b2 & "' y='" & _lblY & "' text-anchor='middle'>" & FORMAT ( _t2, _tFmt ) & "</text>" &
    "<text x='" & _b3 & "' y='" & _lblY & "' text-anchor='middle'>" & FORMAT ( _t3, _tFmt ) & "</text>"
```

### Variations

**Apply the same rule to the value label.** The current value usually has its own precision, so it's handled separately. To trim its trailing zeros as well, add:

```dax
VAR _vFmt =
    IF ( ROUND ( _val, 0 ) = ROUND ( _val, 2 ), "0",
        IF ( ROUND ( _val, 1 ) = ROUND ( _val, 2 ), "0.0", "0.00" ) )
```

and change `FORMAT ( _val, "0.00" )` in `_marker` to `FORMAT ( _val, _vFmt )`.

**Allow up to 3 decimals.** Add a third check comparing at 2 vs 3 decimals, change every `ROUND ( …, 2 )` to `ROUND ( …, 3 )`, and use `"0.000"` as the final fallback.

**One format for the whole table instead of per row.** If you'd rather every bar in the table use the same precision, so the column looks uniform, replace `_allWhole` and `_allTenths` with checks across all visible rows:

```dax
VAR _allWhole =
    ISEMPTY (
        FILTER (
            ALLSELECTED ( Metrics ),
            Metrics[Binary] <> 1
                && (
                    ROUND ( Metrics[Optimized], 0 ) <> ROUND ( Metrics[Optimized], 2 )
                    || ROUND ( Metrics[Stable], 0 ) <> ROUND ( Metrics[Stable], 2 )
                    || ROUND ( Metrics[Monitor], 0 ) <> ROUND ( Metrics[Monitor], 2 )
                )
        )
    )
VAR _allTenths =
    ISEMPTY (
        FILTER (
            ALLSELECTED ( Metrics ),
            Metrics[Binary] <> 1
                && (
                    ROUND ( Metrics[Optimized], 1 ) <> ROUND ( Metrics[Optimized], 2 )
                    || ROUND ( Metrics[Stable], 1 ) <> ROUND ( Metrics[Stable], 2 )
                    || ROUND ( Metrics[Monitor], 1 ) <> ROUND ( Metrics[Monitor], 2 )
                )
        )
    )
```

"No visible row needs more decimals" is the same as "every row can drop them." `ALLSELECTED` respects slicers and filters, so the precision adapts to whatever is currently shown.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| A long text string starting `data:image/svg+xml` appears instead of a picture | Data category isn't set | Click the measure in the Fields pane → **Measure tools → Data category → Image URL** |
| Image URL isn't in the Data category list | Field was selected in a visual, not the Fields pane; or the report is live-connected to a shared semantic model | Select the measure in the Fields pane; check the bottom-right status bar for a live connection |
| Bars look tiny or squashed | Table image size doesn't match the SVG | Set **Image size** to 328 × 46 (or your scaled size) |
| Colored bands overlap or a band is missing | Thresholds are out of order for the row's direction | Check the order rules in [section 1](#expected-columns) |
| Nothing renders on some machines | Region uses commas for decimals, so coordinates are written like `12,5` | Wrap the position values in `FORMAT ( x, "0.0", "en-US" )` |
| Threshold labels overlap | Two thresholds are very close together | Lower the label `font-size`, or increase `_scale` |
| Binary row shows nothing | `Binary_value` is blank | Fill in 0 or 1 |
| Every row shows a bar, including binary ones | `Binary` is stored as text | Change `= 1` to `= "1"` |

---

## 7. Reference: full measure

The current version, before any of the optional changes in sections 3–5.

```dax
Threshold Bar =
VAR _val      = SELECTEDVALUE ( Metrics[Value] )
VAR _opt      = SELECTEDVALUE ( Metrics[Optimized] )
VAR _stb      = SELECTEDVALUE ( Metrics[Stable] )
VAR _mon      = SELECTEDVALUE ( Metrics[Monitor] )
VAR _isHigh   = SELECTEDVALUE ( Metrics[Target Direction] ) = ">"
VAR _isBinary = SELECTEDVALUE ( Metrics[Binary] ) = 1
VAR _binVal   = SELECTEDVALUE ( Metrics[Binary_value] )

-- Colors (%23 = #)
VAR _red    = "%23D64545"
VAR _yellow = "%23F2C94C"
VAR _green  = "%2327AE60"
VAR _blue   = "%232F80ED"

-- ===== Canvas =====
VAR _barW    = 484                 -- bar length (px)
VAR _canvasW = _barW + 28          -- 14 px margin each side
VAR _canvasH = 46
VAR _scale   = 1                   -- 1 = native size; 1.5 = 50% bigger, etc.
VAR _svgSize =
    "viewBox='0 0 " & _canvasW & " " & _canvasH & "' width='" & _canvasW * _scale &
    "' height='" & _canvasH * _scale & "'"

-- ===== Binary icon =====
VAR _pass      = _binVal = 1
VAR _iconColor = IF ( _pass, _green, _red )
VAR _iconMark =
    IF (
        _pass,
        "<path d='M18 23 l3.5 3.5 l6.5 -7' fill='none' stroke='white' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'/>",
        "<path d='M19.5 19.5 l7 7 M26.5 19.5 l-7 7' fill='none' stroke='white' stroke-width='2.2' stroke-linecap='round'/>"
    )
VAR _iconSvg =
    "data:image/svg+xml;utf8," &
    "<svg xmlns='http://www.w3.org/2000/svg' " & _svgSize & " font-family='Segoe UI' font-size='11' fill='%23333333'>" &
    "<circle cx='23' cy='23' r='9' fill='" & _iconColor & "'/>" &
    _iconMark &
    "<text x='38' y='27' font-weight='bold'>" & IF ( _pass, "Pass", "Fail" ) & "</text>" &
    "</svg>"

-- ===== Threshold bar =====
-- Scale: range of value + thresholds, padded 15% each side
VAR _min  = MIN ( MIN ( _val, _opt ), MIN ( _stb, _mon ) )
VAR _max  = MAX ( MAX ( _val, _opt ), MAX ( _stb, _mon ) )
VAR _pad  = IF ( _max = _min, 1, ( _max - _min ) * 0.15 )
VAR _lo   = _min - _pad
VAR _span = ( _max + _pad ) - _lo

-- Layout (pixels)
VAR _left  = 14
VAR _right = _left + _barW
VAR _barY  = 16
VAR _barH  = 14
VAR _lblY  = _barY + _barH + 11

-- Positions
VAR _xOpt = ROUND ( _left + ( _opt - _lo ) / _span * _barW, 1 )
VAR _xStb = ROUND ( _left + ( _stb - _lo ) / _span * _barW, 1 )
VAR _xMon = ROUND ( _left + ( _mon - _lo ) / _span * _barW, 1 )
VAR _xVal = ROUND ( _left + ( _val - _lo ) / _span * _barW, 1 )

-- Boundaries left to right, depending on direction
VAR _b1 = IF ( _isHigh, _xMon, _xOpt )
VAR _b2 = _xStb
VAR _b3 = IF ( _isHigh, _xOpt, _xMon )
VAR _t1 = IF ( _isHigh, _mon, _opt )
VAR _t2 = _stb
VAR _t3 = IF ( _isHigh, _opt, _mon )

-- ===== Smart decimals: fewest decimals (0, 1 or 2) that lose nothing =====
VAR _allWhole =
    ROUND ( _t1, 0 ) = ROUND ( _t1, 2 )
        && ROUND ( _t2, 0 ) = ROUND ( _t2, 2 )
        && ROUND ( _t3, 0 ) = ROUND ( _t3, 2 )
VAR _allTenths =
    ROUND ( _t1, 1 ) = ROUND ( _t1, 2 )
        && ROUND ( _t2, 1 ) = ROUND ( _t2, 2 )
        && ROUND ( _t3, 1 ) = ROUND ( _t3, 2 )
VAR _tFmt =
    IF ( _allWhole, "0", IF ( _allTenths, "0.0", "0.00" ) )

-- ===== Label collision avoidance =====
VAR _s1 = FORMAT ( _t1, _tFmt )
VAR _s2 = FORMAT ( _t2, _tFmt )
VAR _s3 = FORMAT ( _t3, _tFmt )
VAR _chars = MAX ( LEN ( _s1 ), MAX ( LEN ( _s2 ), LEN ( _s3 ) ) )
VAR _gap   = _chars * 6 + 6        -- minimum distance between label centers (px)
VAR _minX  = _chars * 3 + 2        -- keeps labels inside the image
VAR _maxX  = _canvasW - _minX

-- 1. Push the outer labels away from the middle one
VAR _p1 = MIN ( _b1, _b2 - _gap )
VAR _p3 = MAX ( _b3, _b2 + _gap )
-- 2. If that went off the left edge, slide the group right
VAR _q1 = MAX ( _p1, _minX )
VAR _q2 = MAX ( _b2, _q1 + _gap )
VAR _q3 = MAX ( _p3, _q2 + _gap )
-- 3. If that went off the right edge, slide the group left
VAR _x3 = MIN ( _q3, _maxX )
VAR _x2 = MIN ( _q2, _x3 - _gap )
VAR _x1 = MIN ( _q1, _x2 - _gap )

-- Segment colors left to right
VAR _c1 = IF ( _isHigh, _red,    _blue )
VAR _c2 = IF ( _isHigh, _yellow, _green )
VAR _c3 = IF ( _isHigh, _green,  _yellow )
VAR _c4 = IF ( _isHigh, _blue,   _red )

VAR _rects =
    "<rect x='" & _left & "' y='" & _barY & "' width='" & ( _b1 - _left ) & "' height='" & _barH & "' fill='" & _c1 & "'/>" &
    "<rect x='" & _b1 & "' y='" & _barY & "' width='" & ( _b2 - _b1 ) & "' height='" & _barH & "' fill='" & _c2 & "'/>" &
    "<rect x='" & _b2 & "' y='" & _barY & "' width='" & ( _b3 - _b2 ) & "' height='" & _barH & "' fill='" & _c3 & "'/>" &
    "<rect x='" & _b3 & "' y='" & _barY & "' width='" & ( _right - _b3 ) & "' height='" & _barH & "' fill='" & _c4 & "'/>"

VAR _labels =
    "<text x='" & _x1 & "' y='" & _lblY & "' text-anchor='middle'>" & _s1 & "</text>" &
    "<text x='" & _x2 & "' y='" & _lblY & "' text-anchor='middle'>" & _s2 & "</text>" &
    "<text x='" & _x3 & "' y='" & _lblY & "' text-anchor='middle'>" & _s3 & "</text>"

VAR _marker =
    "<line x1='" & _xVal & "' x2='" & _xVal & "' y1='" & ( _barY - 3 ) & "' y2='" & ( _barY + _barH + 3 ) &
    "' stroke='%23222222' stroke-width='2.5'/>" &
    "<text x='" & _xVal & "' y='" & ( _barY - 5 ) & "' text-anchor='middle' font-weight='bold'>" &
    FORMAT ( _val, "0.00" ) & "</text>"

VAR _barSvg =
    "data:image/svg+xml;utf8," &
    "<svg xmlns='http://www.w3.org/2000/svg' " & _svgSize & " font-family='Segoe UI' font-size='10' fill='%23333333'>" &
    _rects & _labels & _marker &
    "</svg>"

-- ===== Output =====
RETURN
SWITCH (
    TRUE (),
    _isBinary && ISBLANK ( _binVal ), BLANK (),
    _isBinary, _iconSvg,
    ISBLANK ( _val ), BLANK (),
    _barSvg
)
```