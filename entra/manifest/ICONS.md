# Icons — `color.png` and `outline.png`

The manifest references two PNG files that must sit **beside `manifest.json` at
the root of the app package zip**, not in a subfolder. They are not optional:
`icons` is a required property in the manifest schema, and the Teams client
rejects a package whose icons are missing, misnamed, or the wrong dimensions.

These two files are **not** in this repository. Binary placeholders would be
worse than useless — a zero-byte or wrongly-sized PNG produces an upload error
that reads like a manifest problem and sends you looking in the wrong place.
Produce them from the spec below.

## Specification

| | `color.png` | `outline.png` |
| --- | --- | --- |
| **Dimensions** | **192 × 192 px**, exactly | **32 × 32 px**, exactly |
| **Format** | PNG | PNG |
| **Colour** | Full colour | **White and transparent only** |
| **Transparency** | Allowed | Required for the background |
| **Filename** | must match `icons.color` in the manifest | must match `icons.outline` |
| **Where used** | App store listing, app bar, install dialog, "About" | App bar when the app is active, activity feed, some monochrome surfaces |

### `color.png` — the full-colour icon

- Exactly 192 × 192 pixels. Not 191, not 256, not "192 at 2x".
- Teams renders it inside a rounded shape and applies its own masking. Keep the
  meaningful content within roughly the central 96 × 96, or it gets clipped.
- A transparent background is fine and usually looks better than a hard square.
- Full colour palette, no restriction.

### `outline.png` — the monochrome icon

This is the one people get wrong. It is **not** a small copy of the colour icon.

- Exactly 32 × 32 pixels.
- **Only two kinds of pixel: white (`#FFFFFF`) and fully transparent.** No greys,
  no colour, no anti-aliased soft edges bleeding a colour in, no white
  background.
- Teams recolours this glyph to match the current theme. A non-transparent
  background renders as a solid white block in dark mode. A coloured outline
  icon renders as mud.
- Keep the glyph inside a 32 × 32 canvas with a small margin — roughly a
  28 × 28 live area.
- Design it as a silhouette. Fine detail disappears at this size.

## Constraints worth knowing before you design

- **File size.** Keep both well under 1 MB. The whole app package should be a
  few hundred KB at most; there is no reason for an icon to be large.
- **No animation.** Animated PNG is not supported.
- **Filenames are case-sensitive** on the packaging path. `Color.png` will not
  match `"color": "color.png"`.
- **Zip layout is flat.** `manifest.json`, `color.png` and `outline.png` all sit
  at the top level of the zip. If your zip tool wraps everything in a folder,
  the upload fails. See [../03_teams_app_manifest.md](../03_teams_app_manifest.md)
  for the correct zip command.
- **The accent colour is separate.** `accentColor` in `manifest.json` (currently
  `#2C5F9E`) is the background Teams paints behind the outline icon on some
  surfaces. Pick one with enough contrast against white, and remember the
  outline icon's white glyph is what sits on top of it.

## Ways to produce them

**Teams Developer Portal** (easiest, no tooling): create the app there, go to
*Branding*, and it will accept an upload and tell you immediately if the
dimensions are wrong. It also generates a serviceable default pair if you have
no artwork, which is fine for a sandbox demo.

**ImageMagick**, from an existing square logo:

```bash
# colour icon
magick input-logo.png -resize 192x192! -background none color.png

# outline icon: flatten to a white silhouette on transparency
magick input-logo.png -resize 32x32! -alpha extract \
  -threshold 50% -negate -transparent black \
  -fill white -colorize 100 outline.png
```

Verify before packaging — this catches the majority of icon failures:

```bash
magick identify color.png outline.png
# expect:  color.png PNG 192x192 ...
#          outline.png PNG 32x32 ...
```

And confirm the outline really is white-and-transparent only:

```bash
magick outline.png -format "%[colorspace] unique-colours:%k\n" info:
# a correct outline icon reports a very small number of unique colours
```
