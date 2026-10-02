# Frontend template and view contract

Read this file for template context, URL names and form states. Page requirements
are in the [page map](wiki/page-map.md); calculations, submission and security
rules are in the [order flow](wiki/order-flow.md). Select other documents from
the [project index](../AGENTS.md) as needed.

The public pages use Django templates. Home, information, category, and product
views load their data from PostgreSQL. Home and the shared assortment menu list
categories that contain published products; category and product pages show only
published products. The anonymous retail cart is stored in the server session;
its add, update, and remove actions validate on the server. Request creation and
the wholesale form remain disabled and do not save requests; their direct POST
returns HTTP 503.

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
| `home` | `home.html` | `catalog_ready`, all public `categories`, `featured_products`, optional `hero_photo`, `home_title`, `home_sections`, `hero_section`, `delivery_section`, `home_steps`, `about_summary`, `advantages`, and `contact_summary`. Category cards use published records; home text comes from fixed content blocks. Company and contact text must be confirmed by the client. |
| `catalog:category` | `catalog/category.html` | A real `category`, its directly assigned published `products`, breadcrumbs for home/current category, optional `page_obj` and `catalog_error`. Resolve unknown slugs as 404; do not expose unpublished products. |
| `catalog:product` | `catalog/product.html` | A real published `product`, `forms_enabled=True`, `field_errors`, optional `field_ids`, `initial_weight`, `is_in_cart`, and breadcrumbs. Resolve unknown or unpublished products as 404. Adding the same product replaces its desired weight. |
| `orders:cart` | `orders/cart.html` | Current `cart_items`, `forms_enabled=True`, `checkout_enabled=False`, optional `cart_total`, `cart_total_is_valid`, `cart_error`, generic `cart_issues`, and breadcrumbs. |
| `orders:checkout` | `orders/checkout.html` | Read-only current `cart_items`, optional server-computed `cart_total`, `cart_total_is_valid`, `forms_enabled=False`, generic `cart_issues`, and breadcrumbs. POST still returns 503. |
| `orders:success` | `orders/success.html` | `order_number` only after resolving the unguessable URL token to a saved order. Unknown tokens return 404. Never include customer details. |
| `inquiries:wholesale` | `inquiries/wholesale.html` | `forms_enabled`, optional submitted `values`, `field_errors`, and form-level `form_errors`. |
| `content:about`, `content:delivery`, `content:contacts`, `content:privacy` | `content/page.html` | `page_lead` as the optional lead section (title/body), and `content_sections` as an ordered sequence of `{title, body}` or an accurate `content_notice` while client material is missing. |

## Product and cart data

`categories` contains `{name, slug}` records, optionally `image` with `url` and
`alt_text`. The homepage displays all categories that have published products,
up to 2 featured products, and up to 4 confirmed advantages. The shared header
menu exposes the same categories on every page and links back to the homepage
assortment section. Category pages contain each category's published products.
`hero_photo` is an optional client-approved photograph of prepared fish product
with `{url, alt_text}`. Keep it empty until that real photograph is supplied;
the hero then renders an honest text fallback instead of stock photography.
A product card requires
`{name, slug}` and may include `short_description`, `price_per_kg` as `Decimal`,
and `primary_photo` with `url` and `alt_text`. A product page additionally
needs `id`, `description`, ordered `photos`, `minimum_weight`, and
`weight_step`. The view must provide only published products and must calculate
all money values on the server. Browser weight and price fields are input hints,
not trusted values.

Each `cart_items` entry contains a stable numeric product-ID `key`, a currently
published `product`, its current server-verified `price_per_kg`, desired `weight`,
`minimum_weight`, `weight_step`, and server-calculated `line_total`. The session
stores only string product IDs and string weights; it stores no price history.
Adding the same product replaces its weight. The total is the sum of rounded line
totals in `cart_total`. If the current minimum or step no longer accepts a stored
weight, keep the line and attach a `weight_errors` message. Omit that line's
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

`forms_enabled` defaults to false. The retail product and cart handlers are
enabled for session-only cart changes; the checkout and wholesale forms stay
disabled. Cart weight fields use the product's current minimum and exact decimal
step, CSRF protection, and a post/redirect/get flow. On the cart page, a small
JavaScript enhancement saves weight after a short pause in typing (or immediately
on change/Enter) and refreshes the server-calculated line amounts and total without
navigation. Requests, including removal, run sequentially within the page. Pending
or invalid weights hide their line amount and the aggregate total; the input and
focus remain in place. There is no Update button. Without JavaScript, the
`Сохранить вес` submit button provides the same server validation and persistence.
Removing a line bypasses
browser weight validation. The checkout and wholesale POST handlers return 503
and do not persist requests. When a request form is enabled, views can pass `values`, a
`field_errors` mapping keyed by the HTML field names, a `form_errors` sequence,
and an optional `page_error`. Field errors render beside their control and in
the summary. Keep `aria-invalid` and `aria-describedby` attached to errors.

Retail field names: `name`, `phone`, `email`, `pickup_method`, `address`,
`comment`, `consent`, and optional `confirm_updated_cart`. Wholesale field
names: `contact_name`, `company`, `phone`, `email`, `need`, and `consent`.
Address is required by the server only for delivery. The small JavaScript file
only hides the address field and toggles its browser `required` hint; the view
must enforce the delivery rule without JavaScript.

Before creating a retail request, re-read publication and price from the
database and compare them with the server-side snapshot shown for confirmation.
On a mismatch, return the changed composition and require a second explicit
confirmation. Never use browser-supplied totals as order amounts.

`cart_changes` contains already-sanitized `{message}` entries for visible price
changes or generic notices for unavailable items; never expose an unpublished
product name or its data while explaining a cart change.

## URL names

- `home`
- `catalog:category`, `catalog:product`
- `orders:cart`, `orders:checkout`, `orders:success`
- `inquiries:wholesale`
- `content:about`, `content:delivery`, `content:contacts`, `content:privacy`

## Content persistence boundary

Five fixed ContentPage keys map to the existing routes only. The view loads a
page and all sections in two queries, then uses an in-memory key map. No content
cache is configured. The template fixes the home composition; display_order
sorts advantages/steps and information sections, with PK as a stable tie-breaker.
The reserved lead block remains the introduction. Empty sections are not shown
as technical keys. Existing neutral UI fallbacks remain when material is missing.
All stored text is escaped, with paragraph/line-break filters; never apply safe.
See [data-layer.md](data-layer.md) for fixed keys, migrations and ORM boundaries.
