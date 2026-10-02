# Типографика тёмного дерева

Подбор выполнен отдельным subagent GPT-6 Luna с reasoning effort High по прямому запросу пользователя, 27.09.2026.

Выбранная пара — Cormorant Garamond + Golos Text. Cormorant даёт контрастные, утончённые serif-заголовки, Golos обеспечивает читаемый русский интерфейс. Оба семейства уже доступны локально и лицензированы по SIL OFL 1.1; сетевой загрузки шрифтов нет. В публичной теме используются только эти два семейства. Instrument Serif, Work Sans и JetBrains Mono исключены из активного CSS, их исходные файлы сохранены.

| Роль | Семейство | Вес |
|---|---|---|
| Заголовки, названия категорий и товаров | Cormorant Garamond | 400 |
| Основной текст | Golos Text | 400 |
| Навигация | Golos Text | 500 |
| Кнопки и подписи полей | Golos Text | 600 |

Локальные normal/italic WOFF2 Cormorant и normal WOFF2 Golos включают Latin и стандартную русскую кириллицу. Расширенный cyrillic-ext не подключён; upstream предлагает его при необходимости. Лицензии находятся в static/fonts/cormorantgaramond-OFL.txt и static/fonts/golostext-OFL.txt.

Первичные источники: [Cormorant Garamond: метаданные и наборы](https://github.com/google/fonts/blob/main/ofl/cormorantgaramond/METADATA.pb), [Golos Text: назначение и лицензия](https://github.com/googlefonts/golos-text/blob/main/README.md), [Golos Text: метаданные](https://github.com/google/fonts/blob/main/ofl/golostext/METADATA.pb), [SIL OFL](https://openfontlicense.org/).

Рассмотренные альтернативы: [Literata + Golos Text](https://github.com/googlefonts/literata) для более книжной редакционной интонации; [PT Serif + PT Sans](https://github.com/google/fonts/blob/main/ofl/ptserif/DESCRIPTION.en_us.html) для более классического вида. Выбранная пара лучше соответствует гастрономической витрине на тёмном дереве и не требует новых загрузок.
