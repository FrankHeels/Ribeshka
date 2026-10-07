from django import forms

from integrations.public_forms import clean_public_phone


class WholesaleRequestForm(forms.Form):
    contact_name = forms.CharField(max_length=160, label="Контактное лицо")
    company = forms.CharField(max_length=180, required=False, label="Компания")
    phone = forms.CharField(max_length=40, label="Телефон")
    email = forms.EmailField(max_length=254, required=False, label="Электронная почта")
    consent = forms.BooleanField(required=True, label="Согласие на обработку данных")
    website = forms.CharField(required=False, max_length=200, strip=True, label="")
    submission_key = forms.UUIDField(widget=forms.HiddenInput)
    submission_token = forms.CharField(max_length=1000, widget=forms.HiddenInput)

    def clean_phone(self):
        phone = clean_public_phone(self.cleaned_data["phone"])
        if phone is None:
            raise forms.ValidationError(
                "Укажите телефон: от 5 до 15 цифр; допустимы начальный +, пробелы, скобки, точка и дефис."
            )
        return phone
