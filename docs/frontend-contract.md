# Public frontend contract

This document defines visible public-page behavior and interaction states. Route ownership is summarized in [`wiki/page-map.md`](wiki/page-map.md). Retail cart/request and consent rules belong in [`wiki/cart.md`](wiki/cart.md) and [`wiki/public-forms.md`](wiki/public-forms.md); keep detailed domain rules there instead of copying them here.

## Shared page shell

- Breadcrumb markup is shared through `partials/breadcrumbs.html`. Information pages (wholesale, about, delivery, contacts) override the base breadcrumb block and render the partial inside their photo header; other pages keep the base placement.
- Public pages are Django-rendered. `templates/base.html` owns the document language and viewport, title/description/robots metadata, stylesheet, skip link, header, breadcrumbs, footer, and shared messages.
- The shared `<head>` includes the `yandex-verification` and `google-site-verification` meta tags for site ownership verification in Yandex Webmaster and Google Search Console.
- The desktop header and native `<details>` mobile menu expose assortment, wholesale, about, delivery, contacts, and cart. Keep the menu usable without JavaScript; enhancements must preserve keyboard and touch behavior. The cart link has an accessible name and optional line count.
- The header does not display a phone number. The footer contains links to assortment, about, delivery, contacts, and all three legal documents; do not add contacts or social links there without changing the approved design.
- `page_title`, optional `seo_title`, `meta_description`, and `meta_robots` feed shared metadata. The session-specific cart, request-result pages, legal pages, and errors as appropriate use `noindex, nofollow`. Normal GET/HEAD/POST responses from the cart view, its database fallback, and request results also carry `X-Robots-Tag: noindex, nofollow` and private no-store headers.
- `canonical_url` adds one absolute `rel="canonical"` link for home, wholesale, about, delivery, contacts, and public catalog pages. The SEO context processor uses the resolved route without query parameters; the category view overrides it with the actual paginator page (`?page=N` only for pages after the first). Tracking and other unrelated parameters are omitted. Pages whose `meta_robots` contains `noindex` do not render a canonical link, including errors and legal/request-result pages; `index, follow` permits the link. URL scheme and host follow the same trusted request/proxy configuration as sitemap.

## Template context

The route view or its presentation function owns each page context. Context processors add common values. When a template contract changes, use those functions as the source of truth and update this table; do not copy backend calculations here.

| Route / template | Values passed to the template |
|---|---|
| All public templates | `navigation_categories`: `QuerySet[Category]` with a published, unblocked product; `cart_count`: integer number of distinct public cart lines; `site_settings`: `SiteSettings` or `None`; `site_links`: `QuerySet[SiteLink]` with a nonblank URL. The home view supplies its prefetched navigation categories directly. |
| `/` — `home.html` | `home_title: str`, `catalog_ready: bool`, `categories: QuerySet[Category]` (each with prefetched `published_products`), `featured_products: list[Product]`, `hero_values: list[dict[str, str]]` (`icon`, `text`), `about_summary: str`, and shared page metadata. |
| `/catalog/categories/<slug>/` — `catalog/category.html` | `category: Category`, `products: QuerySet[Product]` for the current page, `page_obj: Page`, and shared page metadata/breadcrumbs. The view orders records and paginates 12 per page. |
| `/catalog/products/<slug>/` — `catalog/product.html` | `product: Product`, `forms_enabled: bool`, `initial_weight` (numeric value; default is one kilogram), `field_errors: dict[str, list[str]]`, and shared page metadata/breadcrumbs. Invalid-weight responses may also pass `field_ids` and cart-issue context; `initial_weight` may then contain the submitted value. |
| `/cart/` — `orders/cart.html` | `cart_items: list[dict]` (`key: str`, `product: Product`, `price_per_kg: Decimal`, `weight: Decimal`, `weight_display: str`, `weight_errors: list[str]`, `line_total: Decimal or None`); `cart_total: Decimal or None`, `cart_weight: Decimal or None` (sum of valid desired kilograms; absent on invalid/bound weight errors), `cart_total_is_valid: bool`, `cart_issues: bool`, `cart_issues_message: str`, `cart_error: str or None`, `values: dict`, `field_errors: dict[str, list[str]]`, `form_errors: list[str]`, `price_changed: bool`, `cart_changes: list[dict[str, str]]`, `submission` (`submission_key`, `submission_token`, `consent_url`), and breadcrumbs. On a bound weight error, an item also has the raw `weight_input_value` and `weight_is_bound: bool`. |
| `/wholesale/` — `inquiries/wholesale.html` | `forms_enabled: bool`, `values: dict`, `field_errors: dict[str, list[str]]`, `form_errors: list[str]`, `page_error: str or None`, `submission` (`submission_key`, `submission_token`, `consent_url`), and breadcrumbs. |
| `/about/`, `/delivery/`, `/contacts/` | `page_body: str`, `content_notice: str`, breadcrumbs and shared page metadata. Delivery additionally gets `yandex_maps_api_key: str`; contacts additionally gets `contact_location: dict` and the map key. The templates serialize the key only when nonempty. |
| Legal routes — `content/legal.html` | `legal_html` (renderer-produced safe HTML), `legal_draft_notice: str`, `legal_navigation: list[dict]`, breadcrumbs and `meta_robots`. The archived consent route additionally passes `legal_revision: ConsentRevision`. |
| Request-result routes | Only generic `page_title` and robots metadata are added to the shared context; no request object or identifier is rendered. |

Sources: [`content/views.py`](../content/views.py), [`catalog/views.py`](../catalog/views.py), [`orders/cart/presentation.py`](../orders/cart/presentation.py), [`inquiries/request_flow/presentation.py`](../inquiries/request_flow/presentation.py), [`orders/views.py`](../orders/views.py), and the common context processors in `catalog/`, `orders/`, and `content/`.

## Home and assortment

- `/` has a photographic hero, compact benefits, category assortment, up to two configured recommendation slots, a short company section, the request steps, and delivery/contact panels. The hero action jumps to `#assortment`; there is no public aggregate `/catalog/` page.
- Home categories and recommendations use only public records. Missing category imagery falls back to a sample product image, then to a text placeholder. A missing public assortment gets a visible empty state.
- The hero notice always says prices are indicative, the manager confirms availability and final cost, and a request does not reserve stock. Keep this next to the primary action and readable on narrow screens.
- The home company copy comes from the single editable home body. The company section uses a cream surface; its supporting note remains selectable HTML without the old paper artwork. The home contact panel links to the contact page and renders configured site links; full contact details stay on `/contacts/`.
- Category pages list public products and paginate. Product cards show the current public price as indicative. Product detail shows the product’s price/weight information and explains that the manager confirms current price and availability.

## Product weight and cart controls

- Product and cart weight inputs are whole kilograms from 1 to 99,999, step 1. Provide manual entry, numeric input mode, and labelled decrement/increment buttons. Hide the buttons until their JavaScript enhancement is ready; the form must remain usable without JavaScript.
- On the cart page, JavaScript saves a valid edited weight as the visitor changes it. Process line updates sequentially; a stale response must not replace the result for a newer edit. Keep current amounts stable while a valid edit is pending; hide amounts that are invalid or cannot be confirmed and announce the total state through the existing polite live region.
- Without JavaScript, `Сохранить вес` submits a change. There is no separate Update button. Removing a line remains available when its weight field is invalid. Keep field and removal errors beside their controls and preserve the visitor’s input and focus during updates.
- Cart photos are prefetched by the presentation layer. The cream summary shows the line count and server-confirmed combined desired weight; JavaScript reconciles these alongside the total and hides the weight summary while an invalid or failed update makes totals unavailable.
- Keep the preliminary total visibly qualified: the manager confirms availability, actual weight, delivery cost, and final amount. Do not present it as a confirmed charge.
- `wiki/cart.md` owns session/cart values, pricing and confirmation rules. This section records only the UI behavior.

## Forms and accessible feedback

- Retail request submission is on `/cart/`; `/checkout/` GET redirects to the cart form. Wholesale uses `/wholesale/`. Keep both forms server-rendered with CSRF fields and usable without JavaScript.
- Show a linked, keyboard-focusable error summary when a form has errors. Keep field errors adjacent to inputs and connect them with `aria-describedby` and `aria-invalid`. Use visible labels, semantic buttons, and visible keyboard focus.
- The retail form includes the delivery/self-pickup choice and contact fields; its address control may be enhanced for the selected method, but the page must still explain and submit correctly without JavaScript. The wholesale form has its own fields and success state. Its introductory copy explains product quality, collaboration and the next step: a manager contacts the visitor personally with the current wholesale price list and discusses supply conditions. Submitting the form does not automatically download a price list or confirm a supply agreement.
- The two success routes render generic messages only. Do not show an order/request number, database ID, external identifier, personal contact data, or hidden identifier in the HTML or headers.
- Retain the skip link, semantic headings and landmarks, meaningful image alternatives, and live announcements for asynchronous updates. Do not move focus for routine cart recalculation.

## Information pages and maps

- About and delivery render the corresponding editable content-page body once, with paragraph formatting and escaping, as their introduction. If it is empty, an authored introduction replaces the generic pending-materials notice. Fixed editorial sections remain available alongside that introduction: about describes the assortment, quality and personal service with one thematic fish image; delivery explains weekday courier delivery and self-pickup by agreement. The image illustrates the product theme and is not presented as a photograph of the company's premises.
- The contacts page renders configured primary phone, email, site links, hours, and address. Its map uses the separately configured fixed contact location below those details; keep address and external map links available if the embed fails.
- The delivery page places its map after the text. The static legend lists indicative delivery costs: blue Kaliningrad 300 ₽, red outer zone 500 ₽, green Guryevsk 700 ₽. These figures do not calculate a cart fee; the manager confirms the address, delivery time and final cost. Below the map, a short company statement explains delivery by its own couriers Monday through Friday, at an agreed time. Keep this information, the static zone legend and textual fallback when the map is unavailable. See [`delivery-map.md`](delivery-map.md) for map behavior and provenance.
## Legal pages

- Legal bodies come from the allowlisted Markdown files in `docs/legal/`. Their content is runtime text: do not edit or normalize those files during Wiki maintenance. The renderer escapes raw HTML, rewrites known local legal links, and supports semantic tables with a keyboard-focusable narrow-screen wrapper.
- The legal template displays the “Проект документа” notice on ordinary and archived-revision pages. Archived consent content is preserved separately with its identifier, timestamp, and SHA-256; do not imply that this notice or the archived route constitutes legal approval.
- Consent form links resolve to the exact archived revision used by the submission flow. The privacy policy is a separate informational link. The Metrica consent page is legal text only; it does not establish that a counter, banner, cookie preference, or tracker is implemented. Yandex Maps is a separate feature.
- Legal pages currently use `noindex, nofollow` and are excluded from the sitemap. Filling in the Markdown alone does not remove the project notice, robots directive, or sitemap exclusion. After the text is approved, update the renderer/view metadata, template notice, and sitemap deliberately.

## Visual implementation

Responsive behavior, typography, CSS build and static asset versioning are owned by [`design-system.md`](design-system.md).
