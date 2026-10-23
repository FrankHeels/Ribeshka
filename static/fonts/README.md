# Local fonts

The active storefront uses Cormorant Garamond, Golos Text and Onest.
WOFF2 files are served locally; declarations, weights and original Unicode
ranges are in [frontend/fonts.css](../../frontend/fonts.css). Interface roles
belong to the [design system](../../docs/design-system.md).

## Sources and licences

Files originate from the official Google Fonts CSS API. The unmodified SIL
Open Font License 1.1 for each family is included alongside its local files:

| Family | Local licence | Upstream |
|---|---|---|
| Cormorant Garamond | [OFL](cormorantgaramond-OFL.txt) | [Metadata and subsets](https://github.com/google/fonts/blob/main/ofl/cormorantgaramond/METADATA.pb) |
| Golos Text | [OFL](golostext-OFL.txt) | [Project](https://github.com/googlefonts/golos-text), [metadata](https://github.com/google/fonts/blob/main/ofl/golostext/METADATA.pb) |
| Onest | [OFL](Onest-OFL.txt) | [Source and licence](https://github.com/google/fonts/tree/main/ofl/onest) |

Onest 500 upright Latin/Cyrillic files were obtained from the
[family CSS](https://fonts.googleapis.com/css2?family=Onest:wght@500&display=swap).
The contact phone uses the existing Cormorant Garamond 600 upright face.
For redistribution or replacement, retain the corresponding licence and
check the included glyph ranges against the text being displayed.

Instrument Serif, Work Sans and JetBrains Mono files remain in the directory
with their OFL texts; they are not loaded by the active font stylesheet.
