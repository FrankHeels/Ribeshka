# Frontend template and view contract

Read this file for template context, URL names and form states. Page requirements
are in the [page map](wiki/page-map.md); calculations, submission and security
rules are in the [order flow](wiki/order-flow.md). Select other documents from
the [project index](wiki/INDEX.md) as needed.

The public pages use Django templates. Home, information, category, and product
views load their data from PostgreSQL. Home and the shared assortment menu list
categories that contain published products; category and product pages show only
published products. The anonymous retail cart is stored in the server session;
its add, update, and remove actions validate on the server. Retail and wholesale
request forms are enabled. Retail submission is part of the cart;
wholesale submission collects contacts without a description. Services persist requests
and outbox tasks atomically, emit transactional pg_notify, and resolve duplicate keys.
SMTP delivery and the LISTEN worker remain a later stage.

## Shared page context

- `page_title`: page heading and document title prefix.
- `seo_title`: optional stored document title; falls back to page_title/brand.
- `meta_description`: concise, factual description for the page `<meta>` tag.
- `meta_robots`: optional robots directive; use `noindex, nofollow` for errors.
- `breadcrumbs`: ordered list of `{label, url?}`; omit on the homepage.
- `cart_count`: number of distinct currently published product lines in the server-session cart.
- `messages`: optional Django messages when the messages app is configured.
- `navigation_categories`: categories with at least one published product, ordered
  by `display_order`, name, and primary key. The shared assortment menu uses them
  on every page.

The base template owns the document language, viewport, shared navigation,
footer, skip link, breadcrumb navigation, stylesheet, and metadata.
Provide category and product titles and descriptions from published records,
add only published category/product URLs to the sitemap, and supply appropriately
sized images with meaningful alt text. There is no public `/catalog/` index page.

## Route-specific context

| Route name | Template | Required context |
|---|---|---|
| `home` | `home.html` | `catalog_ready`, all public `categories`, `featured_products`, `home_title`, `home_sections`, `hero_section`, `delivery_section`, `home_steps`, `about_summary`, `advantages`, and `contact_summary`. Category cards use published records; home text comes from fixed content blocks. Company and contact text must be confirmed by the client. |
| `catalog:category` | `catalog/category.html` | A real `category`, its directly assigned published `products`, breadcrumbs for home/current category, optional `page_obj` and `catalog_error`. Resolve unknown slugs as 404; do not expose unpublished products. |
| `catalog:product` | `catalog/product.html` | A real published `product`, `forms_enabled=True`, `field_errors`, optional `field_ids`, `initial_weight`, and breadcrumbs. The initial weight is always 1 kg to add; invalid POST responses preserve the submitted value. The legend and button always say `Добавить в корзину`. Resolve unknown or unpublished products as 404. Adding the same product increases the line's weight. |
| `orders:cart` | `orders/cart.html` | Current `cart_items`, `forms_enabled=True`, server `cart_total`, `cart_total_is_valid`, `cart_error`, generic `cart_issues`, `submission`, optional `values`, `field_errors`, `form_errors`, `price_changed`, `cart_changes`, and breadcrumbs. The retail contact form is below the total. |
| `orders:checkout` | Redirect / cart handler | GET redirects to `/cart/#request`; POST uses the same server-side retail submission handler. The old checkout template is not a separate public screen. |
| `orders:success` | `orders/success.html` | `order_number` only after resolving the unguessable URL token to a saved order. Unknown tokens return 404. Never include customer details. |
| `inquiries:wholesale` | `inquiries/wholesale.html` | `forms_enabled=True`, signed `submission`, optional submitted `values`, `field_errors`, and form-level `form_errors`. |
| `inquiries:success` | `inquiries/success.html` | `inquiry_number` only, after resolving the separate unguessable UUID token; unknown/malformed tokens return 404. |
| `content:about`, `content:delivery`, `content:contacts` | `content/page.html` (`delivery` extends it) | `page_lead` as the optional lead section (title/body), and `content_sections` as an ordered sequence of `{title, body}` or an accurate `content_notice` while client material is missing. |
| `content:privacy`, `content:personal_data_consent`, `content:yandex_metrica_consent` | `content/legal.html` | `legal_html` rendered from allowlisted repository Markdown, `legal_draft_notice`, `legal_navigation` with current-page indication, breadcrumbs and draft `meta_robots`. Privacy keeps CMS title/SEO; its body always comes from Markdown. |
| `content:consent_revision` | `content/legal.html` | Exact archived consent text plus `legal_revision` with registered identifier, timestamp and SHA-256; unknown IDs return 404. |

## Product and cart data

`categories` contains `{name, slug}` records, optionally `image` with `url` and
`alt_text`. The homepage displays all categories that have published products,
up to 2 featured products, and up to 4 confirmed advantages. The shared header
menu exposes the same categories on every page and links back to the homepage
assortment section. Category pages contain each category's published products.
The hero displays three approved promises with decorative SVG icons: raw-material
quality, own production, and modern standards. Below 900 px they follow the
introductory copy and action. The legacy `hero_photo` context value is unused.
The default hero copy invites an inquiry; editable `hero_section.body` takes precedence.
A product card requires
`{name, slug}` and may include `short_description`, `price_per_kg` as `Decimal`,
and `primary_photo` with `url` and `alt_text`. A product page additionally
needs `id`, `description`, and ordered `photos`. Retail weight is a whole number
of kilograms from 1 to 99,999, with a 1 kg step for every product. The legacy
`Product.minimum_weight` and `Product.weight_step` fields do not control public
retail selection. The view must provide only published products and must calculate
all money values on the server. Browser weight and price fields are input hints,
not trusted values.

Each `cart_items` entry contains a stable numeric product-ID `key`, a currently
published `product`, its current server-verified `price_per_kg`, desired `weight`
as `Decimal`, string `weight_display`, and server-calculated `line_total`. The session
stores only string product IDs and string weights; it stores no price history.
Adding the same product increases its weight; updating a cart line sets its
absolute weight. Validate both the added amount and the accumulated weight,
returning a field error without changing that line on overflow or when its
existing weight is fractional. The total is the sum of rounded line
totals in `cart_total`. Display valid whole weights without a decimal part,
including historical values such as `2.000`. If a well-formed old session line
contains a fractional weight, keep the original value in the line
and attach a `weight_errors` message; never silently round it. Omit that line's
`line_total` and the aggregate `cart_total` until the visitor updates it; totals
for other valid lines may remain visible. A changed catalog price is shown
immediately on the next request with a recalculated preliminary sum. An optional
`weight_errors` sequence belongs to that item only.
Never reveal the title or other details of an unpublished item in
a public cart error; use a generic `cart_issues_message` and require the user
to review the cart again.

`advantages` entries on the homepage are confirmed `{title, description?}`
statements. Keep the sequence empty until those statements are supplied.

## Form behavior and errors

Product cart controls and both request forms are enabled. Add/update/remove cart
actions change only the session; `action=submit` posts the retail request from the cart.
The legacy checkout POST delegates to that handler. Product and cart weight controls use `inputmode="numeric"`, `min="1"`,
`max="99999"`, and `step="1"`. The server enforces whole
kilograms from 1 to 99,999. Both screens have decrement/increment buttons with accessible
names, change the whole weight by 1 kg and never silently round invalid input.
They are hidden without JavaScript; manual entry and form submission remain
available. Both inputs are sized for two digits and have no native spinner. Product
buttons only select the added weight; the add submit button saves it. Cart changes
use CSRF protection and a post/redirect/get flow. On the cart page, a small
JavaScript enhancement sends weight changes immediately and refreshes the
server-calculated line amounts and total without navigation. Requests, including
removal, run sequentially within the page. The last confirmed amounts stay visible
while a valid weight is pending; matching server responses replace them instantly
without animation. Stale responses do not overwrite amounts for a newer edited
weight. Invalid weights and failed saves hide their line amount and the aggregate
total while preserving their space; the input and focus remain in place.
The summary and amount containers stay in the DOM: `data-cart-line-total-value`
and `data-cart-total-value` carry server amounts; availability flags are set on
the line amount and the `data-cart-total` container.
Unavailable amounts use `visibility` and `aria-hidden`. The visible
status banner is removed. A visually hidden live region announces the total's
state, and field/removal errors remain visible beside their controls.
There is no Update button. Without JavaScript, the
`Сохранить вес` submit button provides the same server validation and persistence.
Removing a line bypasses
browser weight validation. Cart reconciliation refreshes `submission_key` and
`submission_token` from the server while preserving the contact fields. It also
refreshes the separate confirmation prompt. Retail submit is prevented while
weights are pending, failed, or invalid. Request views pass `values`, a
`field_errors` mapping keyed by the HTML field names, a `form_errors` sequence,
and an optional `page_error`. Field errors render beside their control and in
the summary. Keep `aria-invalid` and `aria-describedby` attached to errors.

Retail field names: `name`, `phone`, `email`, `pickup_method`, `address`,
`comment`, `consent`, and optional `confirm_updated_cart`. Wholesale field
names: `contact_name`, `company`, `phone`, `email`, and `consent`. The `need` field is absent;
the service ignores any browser-supplied description and saves an empty legacy field.
Address is required by the server only for delivery. The small JavaScript file
only hides the address field and toggles its browser `required` hint; the view
must enforce the delivery rule without JavaScript.

Both forms carry hidden `submission_key` (UUID), `submission_token` (signed claim),
and honeypot `website`. The checkbox link uses `submission.consent_url`, which
opens the exact archived revision named inside the claim. Claims include form
kind, a stable server-session nonce and the consent revision ID;
retail claims additionally include the shown product IDs, names, prices, whole
weights, and `requires_confirmation`. Forms in separate tabs have independent
keys/snapshots. Field errors for technical hidden controls become form-level
messages. Phone formatting allows 5–15 digits, an initial +, spaces, parentheses,
a dot and a hyphen; address/comment/company lengths are checked server-side.
Validation and cart-confirmation re-renders preserve the signed revision and link.
An invalid/unknown revision or a legacy token without a revision returns a fresh
form with consent unchecked and cannot create a request. A successful idempotent
retry resolves the already accepted request before other validation and keeps
its saved revision.

Before creating a retail request, lock products in stable ID order and re-read
publication, name, price, composition and whole weights. Compare with the signed
snapshot. On mismatch, HTTP 409 supplies a new claim and unchecked explicit
confirmation; a confirmation pre-sent with the stale claim cannot authorize it.
Missing confirmation is 400. Empty or invalid carts never create requests.
Browser prices and totals are ignored. Successful creation redirects to the
token URL and clears the retail cart; an accepted key resolves before validation
and rate limiting so a retry returns the same result without clearing a new cart.

CSRF and honeypot protect both forms. PostgreSQL rate windows are shared across
processes and keyed by HMAC of form kind/REMOTE_ADDR; defaults are 20 attempts per
3600 seconds. Missing or invalid CSRF protection returns 403. Limit errors are 429, validation errors 400, temporary persistence
errors 503 with a safe retry message. Isolated persistence failures preserve
values and the form key. A complete database outage uses a 503 renderer without
session access or database-backed context processors. The message does not assert
that an uncertain commit failed; retry the original form with the same key.
Rendered cart/form and request-submission responses use `private, no-store`; success
responses additionally set `noindex, nofollow`. Success pages expose only a number.
Request, retail item snapshots and outbox save in one transaction, with pg_notify
on the same connection. HTTP never waits for SMTP; no email worker is active yet.

`cart_changes` contains already-sanitized `{message}` entries for visible price
changes or generic notices for unavailable items; never expose an unpublished
product name or its data while explaining a cart change.

## URL names

- `home`
- `catalog:category`, `catalog:product`
- `orders:cart`, `orders:checkout`, `orders:success`
- `inquiries:wholesale`, `inquiries:success`
- `content:about`, `content:delivery`, `content:contacts`, `content:privacy`
- `content:personal_data_consent`, `content:yandex_metrica_consent`

## Content persistence boundary

Five fixed ContentPage keys map to the existing routes only. The view loads a
page and all sections in two queries, then uses an in-memory key map. No content
cache is configured. The template fixes the home composition; display_order
sorts advantages/steps and information sections, with PK as a stable tie-breaker.
The reserved lead block remains the introduction. Empty sections are not shown
as technical keys. Existing neutral UI fallbacks remain when material is missing.
All stored text is escaped, with paragraph/line-break filters; never apply safe.
See [data-layer.md](data-layer.md) for fixed keys, migrations and ORM boundaries.

Legal document bodies are the explicit repository-file exception: the fixed
allowlist in `content/legal_documents.py` reads `docs/legal/*.md` and uses
Python-Markdown with tables. Raw HTML is escaped before conversion, local
document links are reversed to site routes, and generated HTML is marked safe.
No request values or CMS body text enter this renderer; placeholders remain
literal text. Document headings, lists and tables are semantic; table wrappers
allow keyboard focus and horizontal scrolling on narrow screens. All documents
are linked in the footer. Retail and wholesale consent checkboxes link the
separate consent document; the policy link is informational text outside the
checkbox label. No analytics permission is bundled into a request form.
The form link targets `/personal-data-consent/revisions/<revision_id>/`; the
route renders the archived source bytes used to make the signed claim, with its
identifier, registration time, and SHA-256.

Delivery extends the shared content template with a map after all CMS sections.
Only this view supplies `yandex_maps_api_key` from `YANDEX_MAPS_API_KEY`; a nonempty
key is serialized with `json_script` and enables the delivery JS module. Zones
remain static client data. See [delivery-map.md](delivery-map.md) for configuration,
reference geometry and graceful failure states.
