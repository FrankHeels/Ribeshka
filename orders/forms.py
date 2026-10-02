from decimal import Decimal

from django import forms


class CartWeightForm(forms.Form):
    weight = forms.DecimalField(
        label="Желаемый вес, кг",
        max_digits=8,
        decimal_places=3,
        error_messages={
            "required": "Укажите желаемый вес.",
            "invalid": "Введите вес числом.",
            "max_digits": "Вес должен содержать не более 8 цифр.",
            "max_decimal_places": "Укажите не более 3 знаков после запятой.",
        },
    )

    def __init__(self, *args, product, **kwargs):
        self.minimum_weight = product.minimum_weight
        self.weight_step = product.weight_step
        super().__init__(*args, **kwargs)
        self.fields["weight"].min_value = self.minimum_weight
        self.fields["weight"].error_messages["min_value"] = (
            f"Минимальный вес — {self.minimum_weight} кг."
        )

    def clean_weight(self):
        weight = self.cleaned_data["weight"]
        if weight < self.minimum_weight:
            raise forms.ValidationError(
                f"Минимальный вес — {self.minimum_weight} кг.",
                code="min_value",
            )
        if (weight - self.minimum_weight) % self.weight_step != Decimal("0"):
            raise forms.ValidationError(
                f"Укажите вес с шагом {self.weight_step} кг от {self.minimum_weight} кг.",
                code="invalid_step",
            )
        return weight
