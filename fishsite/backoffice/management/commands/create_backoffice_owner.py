from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Создать обычного владельца сайта с интерактивным вводом пароля."

    def add_arguments(self, parser):
        parser.add_argument("username", help="Имя новой учётной записи владельца.")

    def handle(self, *args, **options):
        user_model = get_user_model()
        username = options["username"]
        username_field = user_model._meta.get_field(user_model.USERNAME_FIELD)
        try:
            username = username_field.clean(username, None)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc

        lookup = {user_model.USERNAME_FIELD: username}
        if user_model._default_manager.filter(**lookup).exists():
            raise CommandError("Пользователь уже существует. Используйте sync_backoffice_roles для назначения роли.")

        try:
            password = getpass("Пароль владельца: ")
            confirmation = getpass("Повторите пароль: ")
        except (EOFError, KeyboardInterrupt) as exc:
            raise CommandError("Создание отменено.") from exc
        if password != confirmation:
            raise CommandError("Пароли не совпадают.")
        candidate = user_model(**lookup)
        try:
            validate_password(password, user=candidate)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc

        with transaction.atomic():
            user_model._default_manager.create_user(**lookup, password=password)
            call_command("sync_backoffice_roles", username, stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS("Владелец создан. При первом входе в /admin/ настройте TOTP и резервные коды."))
