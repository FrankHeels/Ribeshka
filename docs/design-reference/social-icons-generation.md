# Значки соцсетей и почты

Текущие файлы находятся в `static/img/social/`; для каждого значка есть PNG и SVG:

| Назначение | PNG | SVG |
|---|---|---|
| WhatsApp | `whatsapp-brass-v1.png` | `whatsapp-brass-v1.svg` |
| VK | `vkontakte-brass-v1.png` | `vkontakte-brass-v1.svg` |
| Telegram | `telegram-brass-v1.png` | `telegram-brass-v1.svg` |
| MAX | `max-brass-v1.png` | `max-brass-v1.svg` |
| Почта | `email-brass-v1.png` | `email-brass-v1.svg` |

Полные промпты, ID ревизий Vectorize и параметры хранятся в [`social-icons-generation.json`](social-icons-generation.json). Настройки: один цвет, smoothing 1, прозрачный фон, кадр не обрезается. Итоговый цвет SVG — `#D7B77B`; прозрачные PNG экспортированы из SVG. Название ссылки остаётся доступным текстом в шаблоне и не заменяется изображением.

Для воспроизведения используйте локальные файлы и JSON с промптами и параметрами. Временные preview URL внутри JSON не являются постоянным хранилищем ассетов.
