from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


OWNER_GROUP = "FishSite Owner"
OWNER_PERMISSIONS = {
    "catalog": (
        "view_product", "add_product", "change_product", "change_product_price",
        "view_productimage", "add_productimage", "change_productimage", "delete_productimage",
        "view_category", "add_category", "change_category",
    ),
    "content": (
        "view_contentpage", "change_contentpage",
        "view_contentsection", "add_contentsection", "change_contentsection", "delete_contentsection",
        "view_sitesettings", "change_sitesettings",
        "view_homepagerecommendation", "change_homepagerecommendation",
        "view_consentrevision",
    ),
    "orders": ("view_order", "view_orderitem"),
    "inquiries": ("view_inquiry",),
    "integrations": (
        "view_moyskladproduct", "view_moyskladorderlink", "view_moyskladsyncstate",
        "link_moysklad_product", "create_moysklad_candidate",
        "sync_moysklad_catalog", "reconcile_moysklad_prices", "repair_moysklad_order",
    ),
}


class Command(BaseCommand):
    help = "Синхронизировать прикладную роль владельца для существующей учётной записи."

    def add_arguments(self, parser):
        parser.add_argument("username", help="Имя существующей обычной учётной записи владельца.")

    @transaction.atomic
    def handle(self, *args, **options):
        username = options["username"]
        user_model = get_user_model()
        try:
            user = user_model._default_manager.select_for_update().get(**{user_model.USERNAME_FIELD: username})
        except user_model.DoesNotExist as exc:
            raise CommandError("Учётная запись не найдена. Сначала создайте её в обычном административном процессе.") from exc

        if user.is_superuser:
            raise CommandError("Для владельца сайта нужна отдельная обычная учётная запись, не superuser.")
        if not user.is_active:
            raise CommandError("Нельзя назначить владельцем отключённую учётную запись.")

        permissions = []
        for app_label, codenames in OWNER_PERMISSIONS.items():
            found = list(Permission.objects.filter(content_type__app_label=app_label, codename__in=codenames))
            found_names = {permission.codename for permission in found}
            missing = set(codenames) - found_names
            if missing:
                raise CommandError(
                    "Не найдены права после миграций: "
                    + ", ".join(f"{app_label}.{codename}" for codename in sorted(missing))
                )
            permissions.extend(found)

        group, _ = Group.objects.get_or_create(name=OWNER_GROUP)
        allowed_ids = {permission.pk for permission in permissions}
        other_grants = set(user.user_permissions.values_list("pk", flat=True))
        other_grants.update(
            user.groups.exclude(pk=group.pk).values_list("permissions__pk", flat=True)
        )
        other_grants.discard(None)
        if other_grants - allowed_ids:
            raise CommandError(
                "У аккаунта есть дополнительные права вне роли владельца. "
                "Используйте отдельный обычный аккаунт или проверьте его права; "
                "существующие разрешения не изменены."
            )
        group.permissions.set(permissions)
        if not user.is_staff:
            user.is_staff = True
            user.save(update_fields=("is_staff",))
        user.groups.add(group)

        self.stdout.write(
            self.style.SUCCESS(
                f"Роль «{OWNER_GROUP}» назначена существующему пользователю {user.get_username()}; "
                f"синхронизировано прав: {len(permissions)}. Другие группы пользователя сохранены."
            )
        )
