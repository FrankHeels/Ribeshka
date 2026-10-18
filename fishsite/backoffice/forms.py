from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q


class StaffChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        label = user.get_full_name().strip() or user.get_username()
        if not user.is_active:
            label += " (учётная запись отключена; сохранится только текущее назначение)"
        return label


def assignee_field_for(instance):
    user_model = get_user_model()
    queryset = user_model._default_manager.filter(is_active=True, is_staff=True).order_by(
        user_model.USERNAME_FIELD
    )
    current_id = getattr(instance, "assigned_to_id", None)
    if current_id:
        queryset = user_model._default_manager.filter(
            Q(is_active=True, is_staff=True) | Q(pk=current_id)
        ).order_by(user_model.USERNAME_FIELD)
    return StaffChoiceField(
        queryset=queryset,
        required=False,
        label="Ответственный менеджер",
        help_text=(
            "Сотрудник, который ведёт заявку. Поле необязательное; "
            "при одном владельце его можно оставить пустым."
        ),
    )
