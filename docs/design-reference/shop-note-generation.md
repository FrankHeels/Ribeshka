# Записка рыбной лавки

Активный декоративный фон `.about-note` на главной, созданный 19.10.2026 встроенным `image_gen`. Изображение содержит только бумагу и гравюру рыбы; текст записки остаётся HTML в `templates/home.html`.

- Исходник: `static/img/backgrounds/shop-note-paper-source.png`.
- Публичный файл: `static/img/backgrounds/shop-note-paper.webp`.
- WebP получен сменой кодирования с качеством 85; композиция и размеры сохранены.
- Резервный фон задаёт CSS (`#F0E4CC`). Записка растёт по содержимому; на телефоне декоративный наклон отключается.

## Полный промпт

Use case: stylized-concept. Asset type: decorative background texture for a real seafood shop website note; HTML text will be layered on top, so absolutely NO text or letterforms in the image. Create a square, front-on, full-bleed sheet of fine warm ivory handmade stationery paper, lightly tactile fibres, subtle natural creases near the edges, softly weathered uncoated paper. The paper fills the entire canvas; no tabletop or outer scene, no perspective. Colour palette: pale warm cream #F0E4CC across the central field, with slightly darker soft tan edge patina. Reserve the upper 78 percent of the entire image as almost uniform very light empty paper with extremely subtle texture for maximum dark text readability. In only the lowest 15 percent, centred horizontally, put a small delicate old-fashioned dark walnut engraved drawing of one fish and two small flowing water lines, about 16 percent of the overall canvas width, tasteful coastal fishmonger stationery, simple finely drawn etching. No heavy stains, no burnt edges, no pirate imagery, no ornate frames, no lettering, no logos, no watermark. Elegant, restrained, realistic paper material, gentle diffuse lighting, perfectly flat graphic suitable for a responsive website background.

Keep this prompt and the source image if the active asset is regenerated. After replacement, check legibility and crop at phone width and 200% text zoom.
