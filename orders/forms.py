from django import forms

from integrations.public_forms import clean_public_phone

from .cart.state import (
    INVALID_WEIGHT_MESSAGE,
    MAX_RETAIL_WEIGHT,
    MIN_RETAIL_WEIGHT,
    is_valid_retail_weight,
)


class CartWeightForm(forms.Form):
    weight = forms.DecimalField(
        label="Желаемый вес, кг",
        max_digits=5,
        decimal_places=0,
        min_value=MIN_RETAIL_WEIGHT,
        max_value=MAX_RETAIL_WEIGHT,
        error_messages={
            "required": "Укажите желаемый вес.",
            "invalid": "Введите вес целым числом в килограммах.",
            "max_digits": "Максимальный вес — 99 999 кг.",
            "max_decimal_places": INVALID_WEIGHT_MESSAGE,
            "min_value": "Минимальный вес — 1 кг.",
            "max_value": "Максимальный вес — 99 999 кг.",
        },
    )

    def clean_weight(self):
        weight = self.cleaned_data["weight"]
        if not is_valid_retail_weight(weight):
            raise forms.ValidationError(
                INVALID_WEIGHT_MESSAGE,
                code="invalid_weight",
            )
        return weight


class RetailRequestForm(forms.Form):
    name = forms.CharField(max_length=160, label="Имя")
    phone = forms.CharField(max_length=40, label="Телефон")
    email = forms.EmailField(max_length=254, required=False, label="Электронная почта")
    pickup_method = forms.ChoiceField(
        choices=(("delivery", "Доставка"), ("pickup", "Самовывоз")),
        label="Способ получения",
    )
    address = forms.CharField(max_length=255, required=False, strip=True, label="Адрес доставки")
    comment = forms.CharField(max_length=3000, required=False, strip=True, label="Комментарий")
    consent = forms.BooleanField(required=True, label="Согласие на обработку данных")
    website = forms.CharField(required=False, max_length=200, strip=True, label="")
    submission_key = forms.UUIDField(widget=forms.HiddenInput)
    submission_token = forms.CharField(max_length=200000, widget=forms.HiddenInput)
    confirm_updated_cart = forms.BooleanField(required=False)

    def clean_phone(self):
        phone = clean_public_phone(self.cleaned_data["phone"])
        if phone is None:
            raise forms.ValidationError(
                "Укажите телефон: от 5 до 15 цифр; допустимы начальный +, пробелы, скобки, точка и дефис."
            )
        return phone

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("pickup_method") == "delivery" and not cleaned_data.get("address", "").strip():
            self.add_error("address", "Укажите адрес для доставки.")
        if cleaned_data.get("pickup_method") == "pickup":
            cleaned_data["address"] = ""
        return cleaned_data
