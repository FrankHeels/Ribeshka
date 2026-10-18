"""Owner forms contain editorial choices only, never infrastructure secrets."""

from django import forms
from django.forms import formset_factory

from catalog.models import Category, Product
from integrations.models import MoySkladProduct


class ProductLinkForm(forms.Form):
    product = forms.ModelChoiceField(label="Карточка сайта", queryset=Product.objects.none())
    confirm = forms.BooleanField(label="Подтверждаю выбранную связь")

    def __init__(self, *args, config, remap=False, **kwargs):
        super().__init__(*args, **kwargs)
        products = Product.objects.select_related("category").order_by("name", "pk")
        if remap:
            products = products.filter(moysklad_product__account_id=config.account_id)
        else:
            products = products.filter(moysklad_product__isnull=True)
        self.fields["product"].queryset = products


class CandidateCreateForm(forms.Form):
    category = forms.ModelChoiceField(label="Категория сайта", queryset=Category.objects.order_by("display_order", "name"))
    confirm = forms.BooleanField(label="Создать неопубликованную карточку для дальнейшего оформления")


class ConfirmForm(forms.Form):
    confirm = forms.BooleanField(label="Подтверждаю действие")


class OrderRepairForm(forms.Form):
    reason = forms.CharField(label="Причина действия", max_length=3000, widget=forms.Textarea(attrs={"rows": 3}))
    confirm = forms.BooleanField(label="Подтверждаю действие с передачей этой заявки")


class ExistingOrderForm(OrderRepairForm):
    external_id = forms.UUIDField(label="UUID существующего заказа МойСклад", help_text="Исполнитель проверит аккаунт и состав заказа до сохранения связи.")


class PositionOverrideForm(forms.Form):
    item_id = forms.IntegerField(widget=forms.HiddenInput)
    source = forms.ModelChoiceField(label="Исправленный товар МойСклад", queryset=MoySkladProduct.objects.none(), required=False)

    def __init__(self, *args, sources, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].queryset = sources


PositionOverrideFormSet = formset_factory(PositionOverrideForm, extra=0, max_num=500, validate_max=True)
