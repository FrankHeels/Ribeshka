# Coding standards

## Architecture

- Keep the application a modular Django monolith; preserve the boundaries between `catalog`, `orders`, `inquiries`, `content` and `integrations`.
- Render public pages with Django Templates. Use HTMX and JavaScript as progressive enhancements so core user flows work without JavaScript.
- Add runtime services or frameworks only to meet a concrete requirement.

## Current library and API documentation

When a task asks about a library, framework, SDK, API, CLI tool or cloud service, fetch current documentation with Context7 before answering or implementing. Resolve the library ID first, then query the matching documentation for each distinct concept. Prefer Context7 over web search for library documentation. Do not use it for refactoring, scripts written from scratch, business-logic debugging or general programming concepts.
