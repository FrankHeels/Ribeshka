# Local fonts

The active storefront uses **Lora** for headings and **Manrope** for UI and body text.
Variable WOFF2 weights 400–700, upright Cyrillic and Latin subsets plus an explicit
U+20BD ruble subset are served locally.
Declarations and original Unicode ranges are in [frontend/fonts.css](../../frontend/fonts.css).
Roles belong to the [design system](../../docs/design-system.md).

Obtained 2026-10-27 from the official Google Fonts CSS API with a modern browser
User-Agent; no external font requests occur at runtime.

| Family | Licence | Upstream |
|---|---|---|
| Lora | [OFL 1.1](lora-OFL.txt) | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/lora) |
| Manrope | [OFL 1.1](manrope-OFL.txt) | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/manrope) |

Keep these licences when redistributing the fonts. Cyrillic, Latin, numbers,
the ruble sign and kilogram labels are covered by the included subsets.

Earlier Cormorant Garamond, Golos Text, Onest, Instrument Serif, Work Sans and
JetBrains Mono files and their licences remain available but are not loaded by
the active public stylesheet.
