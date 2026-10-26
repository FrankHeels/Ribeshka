# Карта публичных страниц

Открывайте этот документ, чтобы найти владельца поведения страницы. UI-контракт и контекст шаблонов описаны в [`../frontend-contract.md`](../frontend-contract.md); детали карт — в [`../delivery-map.md`](../delivery-map.md).

| Маршрут | Текущее поведение | Где менять требования |
|---|---|---|
| `/` | Главная: hero, открытые категории, до двух рекомендаций, информация о компании, шаги заявки, получение и контакты | [`../frontend-contract.md`](../frontend-contract.md), `content.views.home`, `templates/home.html` |
| `/catalog/categories/<slug>/` | Открытая категория, до 12 товаров на странице | `catalog.views.category`, `templates/catalog/category.html` |
| `/catalog/products/<slug>/` | Карточка опубликованного товара и выбор желаемого веса | [`cart.md`](cart.md), `catalog.views.product`, `templates/catalog/product.html` |
| `/cart/` | Сессионная корзина и розничная форма заявки | [`cart.md`](cart.md), [`public-forms.md`](public-forms.md), [`../frontend-contract.md`](../frontend-contract.md) |
| `/checkout/` | GET перенаправляет к форме в корзине; POST — совместимый путь отправки розничной заявки | [`public-forms.md`](public-forms.md), `orders.views.checkout` |
| `/wholesale/` | Условия сотрудничества, пояснение о личном ответе с прайс-листом и отдельная контактная форма | [`public-forms.md`](public-forms.md), [`../frontend-contract.md`](../frontend-contract.md), `inquiries.views.wholesale` |
| `/about/` | Текст об ассортименте, качестве и сервисе, тематическое изображение, переходы в ассортимент и опт; редактируемый текст страницы сохраняется во вступлении | [`../frontend-contract.md`](../frontend-contract.md), `content.views.about`, `templates/content/about.html` |
| `/delivery/` | Условия доставки и самовывоза, карта с ориентировочными ценами в легенде и текстовым запасным вариантом | [`../delivery-map.md`](../delivery-map.md), [`../frontend-contract.md`](../frontend-contract.md) |
| `/contacts/` | Контактные данные и карта с текстовым запасным вариантом | [`../delivery-map.md`](../delivery-map.md), [`../frontend-contract.md`](../frontend-contract.md) |
| `/privacy/`, `/personal-data-consent/`, `/personal-data-consent/revisions/<revision_id>/`, `/yandex-metrica-consent/` | Юридические тексты; архивный маршрут показывает выбранную редакцию согласия | [`../frontend-contract.md`](../frontend-contract.md), [`public-forms.md`](public-forms.md), [`../legal/personal-data-policy.md`](../legal/personal-data-policy.md), [`../legal/personal-data-consent.md`](../legal/personal-data-consent.md), [`../legal/yandex-metrica-consent.md`](../legal/yandex-metrica-consent.md) |
| `/checkout/success/<uuid>/`, `/wholesale/success/<uuid>/` | Общие страницы результата без номера или персональных данных; запрещены к индексации и кэшированию | [`public-forms.md`](public-forms.md), `orders.views.success`, `inquiries.views.success` |

Отдельной публичной страницы `/catalog/` нет. `templates/catalog/index.html` не привязан к маршруту и не подтверждает работающий сценарий. Список категорий и товаров формируется на маршрутах категорий и товаров.

В sitemap входят главная, оптовая форма, about/delivery/contacts, опубликованные категории с доступными товарами и опубликованные товары. Юридические страницы и страницы результата исключены. Маршруты и фильтры заданы в [`fishsite/urls.py`](../../fishsite/urls.py), [`catalog/urls.py`](../../catalog/urls.py), [`content/urls.py`](../../content/urls.py), [`orders/urls.py`](../../orders/urls.py), [`inquiries/urls.py`](../../inquiries/urls.py); состав sitemap — в [`content/views.py`](../../content/views.py).
