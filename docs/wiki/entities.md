# Маршрут по сущностям

| Область | Где искать |
|---|---|
| Категории, товары, фотографии и публичная цена | Модели: [catalog/models.py](../../catalog/models.py); ограничения хранения — [слой данных](../data-layer.md); пользовательское поведение — [контракт frontend](../frontend-contract.md). |
| Фиксированные страницы, общие настройки и рекомендации | Модели: [content/models.py](../../content/models.py); редактирование владельцем — [кабинет](../backoffice.md). |
| Розничная заявка и снимок её позиций | Модели: [orders/models.py](../../orders/models.py); розничный путь — [сценарии заявок](order-flow.md), отправка формы и фиксация согласия — [публичные формы](public-forms.md). |
| Оптовое обращение | Модель: [inquiries/models.py](../../inquiries/models.py); форма и сохранение — [публичные формы](public-forms.md). |
| Email-outbox | Модель: [integrations/models.py](../../integrations/models.py); очередь и отправка — [уведомления](notifications.md). |
| МойСклад | Модели: [integrations/moysklad/models.py](../../integrations/moysklad/models.py); правила — [контракт обмена](../MoySklad-plan.md), схема — [карта реализации](../MoySklad-implementation.md), запуск и восстановление — [эксплуатация](../moysklad-operations.md). |
| Редакции согласия | Модель: [content/models.py](../../content/models.py); актуальный источник текста — [источник текста согласия](../legal/personal-data-consent.md); использование в формах — [публичные формы](public-forms.md). |
