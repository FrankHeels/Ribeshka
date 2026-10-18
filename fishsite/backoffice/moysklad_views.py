"""Local diagnostics and authorized enqueue-only owner actions."""

from datetime import timedelta
from functools import wraps
import hashlib

from django.contrib import admin, messages
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Min, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.cache import add_never_cache_headers
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from catalog.models import Product
from content.legal_documents import read_legal_document_source
from integrations.models import (
    MoySkladApiState, MoySkladIncident, MoySkladOrderLink, MoySkladProduct,
    MoySkladSyncRun, MoySkladSyncState, MoySkladTask, MoySkladWebhookInbox,
)
from integrations.moysklad import catalog_sync, controls, order_export, price_sync
from integrations.moysklad.config import get_config
from integrations.moysklad.contracts import MoySkladError, safe_error_code

from .moysklad_forms import (
    CandidateCreateForm, ConfirmForm, ExistingOrderForm, OrderRepairForm,
    PositionOverrideFormSet, ProductLinkForm,
)


STREAM_LABELS = {"catalog": "Каталог", "prices": "Автоматические цены", "export": "Передача заявок", "statuses": "Учётные статусы"}
STREAM_HELP = {
    "catalog": "Вручную обновляет список исходных товаров МойСклад. Не публикует карточки автоматически.",
    "prices": "Обновляет только цены связанных карточек. Не меняет описания, фотографии и остатки.",
    "export": "Передаёт новые заявки с одобренным согласием после начала регистрации. Исторические заявки автоматически не отправляются.",
    "statuses": "Читает учётные статусы заказов МойСклад. Не меняет остатки и не гарантирует наличие товара.",
}
CANDIDATE_ERROR_LABELS = {
    "invalid_price": "Некорректная розничная цена",
    "missing_price": "Не указана цена выбранного розничного прайса",
    "invalid_currency": "Валюта розничной цены не соответствует настроенной",
    "duplicate_price": "Для выбранного розничного прайса указано несколько цен",
    "unsupported_type": "Этот тип номенклатуры пока не поддерживается",
}
ACTION_PERMISSIONS = {
    "check": "integrations.manage_moysklad",
    "catalog": "integrations.sync_moysklad_catalog",
    "prices": "integrations.reconcile_moysklad_prices",
    "start_export": "integrations.manage_moysklad",
    "stream": "integrations.manage_moysklad",
}


def _source_unit_label(source, config):
    if source.unit_id is None:
        return "Не указана"
    if source.unit_id == config.unit_id:
        return "кг"
    return source.unit_name or "Другая единица"


def owner_permission(permission, *, technical=False):
    def decorate(view):
        @wraps(view)
        def protected(request, *args, **kwargs):
            user = request.user
            if not (user.is_active and user.is_staff and callable(getattr(user, "is_verified", None)) and user.is_verified() and user.has_perm(permission) and (not technical or user.is_superuser)):
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return protected
    return decorate


def local_response(request, template, context, *, status=200):
    allowed_context = admin.site.each_context(request)
    allowed_context.update(context)
    response = render(request, template, allowed_context, status=status)
    add_never_cache_headers(response)
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


def configured_target():
    config = get_config()
    if not config.account_id or not config.namespace:
        raise MoySkladError("not_configured")
    return config


def action_error(error, *, technical=False):
    if not technical:
        return "Действие не выполнено. Проверьте данные; если ошибка повторяется, обратитесь к техническому администратору."
    if isinstance(error, MoySkladError):
        code = error.code
    elif isinstance(error, ImproperlyConfigured):
        code = "invalid_configuration"
    else:
        code = "action_conflict"
    return f"Действие не выполнено. Проверьте данные и состояние обмена. Код: {safe_error_code(code)}."


def safe_run(run):
    if run is None:
        return None
    return {
        "id": run.pk, "kind": run.get_kind_display(), "status": run.get_status_display(),
        "reason": run.get_reason_display(), "started_at": run.started_at,
        "completed_at": run.completed_at, "total": run.total_count,
        "processed": run.processed_count, "updated": run.updated_count,
        "skipped": run.skipped_count, "errors": run.error_count,
        "error_code": safe_error_code(run.last_error_code) if run.last_error_code else "",
    }


def _business_services(context, *, state, runs, tasks, incidents):
    """Describe local observations without treating a fresh worker signal as success."""
    now = timezone.now()
    common = []
    heartbeat = state.heartbeat_at if state else None
    if heartbeat is None:
        common.append("Обмен не запущен или ещё не получен сигнал от исполнителя. Нужна проверка технического администратора.")
    elif now - heartbeat > timedelta(minutes=2):
        common.append("Обновления задерживаются: давно нет связи с исполнителем. Нужна проверка технического администратора.")
    if context["api"]["access_error_code"]:
        common.append("Нет доступа к МойСклад. Нужна проверка подключения техническим администратором.")
    paused_until = context["api"]["paused_until"]
    if paused_until and paused_until > now:
        common.append("Обмен временно приостановлен; ожидает возобновления подключения.")

    issues = {stream["key"]: list(common) for stream in context["streams"]}
    for stream in context["streams"]:
        if not stream["enabled"]:
            issues[stream["key"]].append("Обновление отключено. Для включения обратитесь к техническому администратору.")
    if context["api"]["export_paused"]:
        issues["export"].append("Передача заявок приостановлена. Требуется проверка технического администратора.")
    if not state or not state.export_started_at:
        issues["export"].append("Передача новых заявок ещё не запущена.")
    if not state or not state.export_consent_revision_id:
        issues["export"].append("Текущая редакция согласия не одобрена для передачи заявок.")
    else:
        try:
            source = read_legal_document_source("consent")
            current_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        except (OSError, UnicodeError, ImproperlyConfigured):
            issues["export"].append("Не удалось проверить текст согласия. Нужна помощь технического администратора.")
        else:
            if state.export_consent_revision.sha256 != current_hash:
                issues["export"].append("Редакция согласия изменилась; требуется одобрение технического администратора для новых заявок.")
    if not state or not state.prices_reconciled_at:
        issues["prices"].append("Полная проверка цен ещё не завершена.")
    elif now - state.prices_reconciled_at > timedelta(hours=26):
        issues["prices"].append("Полная проверка цен устарела. Обновите цены; если задержка повторяется, нужна помощь технического администратора.")
    for key, kind in (("catalog", "catalog"), ("prices", "prices"), ("statuses", "status")):
        latest = runs.filter(kind=kind).first()
        if latest and latest.status in ("failed", "partial"):
            issues[key].append("Последнее обновление завершено с ошибками и требует внимания.")
    review_kinds = set(tasks.filter(status="manual_review").values_list("kind", flat=True))
    for key, kinds in (("catalog", ("catalog_sync",)),
                       ("prices", ("price", "price_reconciliation")),
                       ("statuses", ("status_sync",))):
        if review_kinds.intersection(kinds):
            issues[key].append("Обновление требует разбора. Нужна помощь технического администратора.")
    if context["export_review_count"]:
        issues["export"].append("Есть заявки, требующие разбора. Откройте передачи заявок для проверки.")
    if context["audit_count"]:
        issues["catalog"].append("Есть опубликованные карточки без связи или с блокировкой. Требуется проверка.")
    incident_scopes = set(incidents.values_list("scope", flat=True))
    for scope in incident_scopes:
        key = {"catalog": "catalog", "prices": "prices", "price": "prices", "export": "export", "order_export": "export", "status": "statuses", "statuses": "statuses"}.get(scope)
        for target in ([key] if key else issues):
            issues[target].append("Есть ошибки обмена. Нужна помощь технического администратора.")
    return {key: {"attention": bool(messages), "messages": messages} for key, messages in issues.items()}


def _moysklad_context(*, technical=False):
    context = {"title": "Технические настройки МойСклад" if technical else "МойСклад", "configured": False}
    try:
        config = configured_target()
    except (ImproperlyConfigured, MoySkladError) as error:
        context["configuration_error"] = "invalid_configuration" if isinstance(error, ImproperlyConfigured) else error.code
        return context

    target = {"account_id": config.account_id, "environment": config.environment}
    state = (
        MoySkladSyncState.objects.filter(**target)
        .select_related("export_consent_revision")
        .defer("export_consent_revision__text")
        .first()
    )
    api = MoySkladApiState.objects.filter(account_id=config.account_id).first()
    tasks = MoySkladTask.objects.filter(**target)
    active_tasks = tasks.filter(status__in=("pending", "processing", "manual_review"))
    sources = MoySkladProduct.objects.filter(account_id=config.account_id, entity_type="product")
    inbox = MoySkladWebhookInbox.objects.filter(**target, processed_at__isnull=True)
    price_tasks = active_tasks.filter(kind="price")
    exports = tasks.filter(kind="export_order", status__in=("pending", "processing"))
    runs = MoySkladSyncRun.objects.filter(**target).order_by("-started_at", "-pk")
    audit_products = Product.objects.filter(is_published=True).filter(
        Q(moysklad_product__isnull=True) | Q(integration_block_reason__gt="")
    ).filter(Q(moysklad_product__isnull=True) | Q(moysklad_product__account_id=config.account_id)).order_by("name", "pk")
    incident_rows = MoySkladIncident.objects.filter(**target, closed_at__isnull=True).order_by("-last_seen_at", "-pk")
    access_check = tasks.filter(kind="check_access").order_by("-created_at", "-pk").first()
    context.update({
        "configured": True, "account_id": config.account_id, "environment": config.environment,
        "state": {
            name: getattr(state, name) if state else None for name in (
                "heartbeat_at", "export_started_at", "webhook_id", "webhook_received_at",
                "webhook_processed_at", "prices_reconciled_at", "prices_next_at",
            )
        },
        "approved_consent": ({
            "identifier": state.export_consent_revision.revision_id,
            "registered_at": state.export_consent_revision.registered_at,
        } if state and state.export_consent_revision_id else None),
        "api": {"access_error_code": safe_error_code((api or state).access_error_code) if (api or state) and (api or state).access_error_code else "",
                "paused_until": (api or state).api_paused_until if (api or state) else None, "export_paused": (api or state).export_paused if (api or state) else False},
        "streams": [{"key": stream, "label": label, "help": STREAM_HELP[stream], "enabled": getattr(state, f"{stream}_enabled") if state and getattr(state, f"{stream}_enabled") is not None else getattr(config, f"{stream}_enabled")} for stream, label in STREAM_LABELS.items()],
        "inbox_count": inbox.count(), "inbox_oldest": inbox.aggregate(oldest=Min("received_at"))["oldest"],
        "price_count": price_tasks.count(), "price_oldest": price_tasks.aggregate(oldest=Min("created_at"))["oldest"],
        "export_count": exports.count(), "export_oldest": exports.aggregate(oldest=Min("created_at"))["oldest"],
        "export_review_count": tasks.filter(kind="export_order", status="manual_review").count(),
        "candidate_count": sources.filter(product__isnull=True).count(),
        "pending_price_count": sources.filter(price_pending_since__isnull=False).count(),
        "pending_price_oldest": sources.aggregate(oldest=Min("price_pending_since"))["oldest"],
        "blocked_count": Product.objects.filter(integration_block_reason__gt="", moysklad_product__account_id=config.account_id).count(),
        "last_price_run": safe_run(runs.filter(kind="prices").first()),
        "last_catalog_run": safe_run(runs.filter(kind="catalog", reason="manual").first()),
        "recent_runs": [safe_run(run) for run in runs[:8]],
        "incidents": [{"id": row.pk, "scope": safe_error_code(row.scope), "code": safe_error_code(row.code), "opened_at": row.opened_at, "last_seen_at": row.last_seen_at} for row in incident_rows[:30]],
        "incident_count": incident_rows.count(),
        "audit_count": audit_products.count(), "audit_products": list(audit_products[:30]),
        "access_check": ({"status": access_check.status, "label": access_check.get_status_display(), "completed_at": access_check.completed_at} if access_check else None),
    })
    if not technical:
        context["services"] = _business_services(context, state=state, runs=runs, tasks=tasks, incidents=incident_rows)
    return context


@require_GET
@owner_permission("integrations.view_moyskladsyncstate")
def moysklad_status(request):
    context = _moysklad_context()
    return local_response(request, "admin/moysklad_status.html", context)


@require_GET
@owner_permission("integrations.view_moyskladsyncstate", technical=True)
def moysklad_technical(request):
    return local_response(request, "admin/moysklad_technical.html", _moysklad_context(technical=True))


@require_POST
@csrf_protect
@owner_permission("integrations.view_moyskladsyncstate")
def moysklad_action(request, action):
    permission = ACTION_PERMISSIONS.get(action)
    technical = action in ("check", "stream", "start_export")
    if not permission or not request.user.has_perm(permission) or (technical and not request.user.is_superuser):
        raise PermissionDenied
    try:
        configured_target()
        if action == "check":
            controls.request_access_check(actor=request.user)
        elif action == "catalog":
            catalog_sync.start_catalog_sync(actor=request.user)
        elif action == "prices":
            price_sync.start_price_reconciliation(reason="manual", actor=request.user)
        elif action == "start_export":
            if request.POST.get("confirm") != "yes":
                raise ValidationError("Подтвердите одобрение текущей редакции согласия для новых заявок.")
            controls.start_export(actor=request.user)
        elif action == "stream":
            if request.POST.get("enabled") not in ("yes", "no"):
                raise ValidationError("Некорректное состояние потока.")
            controls.set_stream_enabled(request.POST.get("stream"), request.POST["enabled"] == "yes", actor=request.user)
    except (ImproperlyConfigured, MoySkladError, ValidationError, ObjectDoesNotExist, IntegrityError) as error:
        messages.error(request, action_error(error, technical=technical))
    else:
        if action == "start_export":
            message = "Текущая редакция согласия одобрена для новых заявок. Дата начала регистрации сохранена. Ранее не разрешённые передачи автоматически не отправляются."
        elif action == "stream":
            message = "Настройка обмена сохранена."
        else:
            message = "Фоновая задача сохранена. Результат появится после обработки исполнителем."
        messages.success(request, message)
    return redirect("admin:moysklad_technical" if technical else "admin:moysklad_status")


@require_GET
@owner_permission("integrations.view_moyskladproduct")
def moysklad_candidates(request):
    context = {"title": "Кандидаты МойСклад", "configured": False}
    try:
        config = configured_target()
    except (ImproperlyConfigured, MoySkladError) as error:
        context["configuration_error"] = action_error(error)
        return local_response(request, "admin/moysklad/candidates.html", context)
    sources = MoySkladProduct.objects.filter(account_id=config.account_id, entity_type="product", product__isnull=True).order_by("name", "pk")
    query = request.GET.get("q", "").strip()[:200]
    if query:
        sources = sources.filter(Q(name__icontains=query) | Q(code__icontains=query) | Q(article__icontains=query))
    page = Paginator(sources, 40).get_page(request.GET.get("page"))
    candidates = []
    for source in page:
        reason = ""
        if source.is_deferred:
            reason = "Отложен для следующего этапа"
        elif source.archived or source.deleted_at:
            reason = "Архивный или удалённый товар"
        elif source.unit_id != config.unit_id:
            reason = "Единица не соответствует настроенному килограмму"
        elif source.last_error_code:
            reason = CANDIDATE_ERROR_LABELS.get(source.last_error_code, safe_error_code(source.last_error_code))
        elif source.last_checked_price is None or source.last_checked_price <= 0:
            reason = "Нет проверенной положительной цены"
        unit = _source_unit_label(source, config)
        candidates.append({"id": source.external_id, "name": source.name, "code": source.code, "article": source.article,
                           "unit": unit, "price": source.last_checked_price, "checked_at": source.price_checked_at, "reason": reason})
    context.update({"configured": True, "candidates": candidates, "page": page, "query": query})
    return local_response(request, "admin/moysklad/candidates.html", context)


@require_http_methods(["GET", "POST"])
@csrf_protect
@owner_permission("integrations.view_moyskladproduct")
def moysklad_candidate_action(request, external_id, operation):
    permission = "integrations.create_moysklad_candidate" if operation == "create" else "integrations.link_moysklad_product"
    if operation not in ("link", "remap", "create") or not request.user.has_perm(permission):
        raise PermissionDenied
    if operation == "create" and not request.user.has_perm("catalog.add_product"):
        raise PermissionDenied
    try:
        config = configured_target()
    except (ImproperlyConfigured, MoySkladError) as error:
        messages.error(request, action_error(error))
        return redirect("admin:moysklad_status")
    source = get_object_or_404(MoySkladProduct, account_id=config.account_id, entity_type="product", external_id=external_id, product__isnull=True)
    data = request.POST if request.method == "POST" else None
    form = CandidateCreateForm(data) if operation == "create" else ProductLinkForm(data, config=config, remap=operation == "remap")
    titles = {"link": "Связать с карточкой сайта", "remap": "Перепривязать существующую карточку", "create": "Создать карточку из кандидата"}
    if request.method == "POST" and form.is_valid():
        try:
            if operation == "create":
                product = catalog_sync.create_product_from_candidate(external_product_id=external_id, category_id=form.cleaned_data["category"].pk, actor=request.user)
            else:
                product = form.cleaned_data["product"]
                service = catalog_sync.remap_product if operation == "remap" else catalog_sync.link_product
                service(product_id=product.pk, external_product_id=external_id, actor=request.user)
        except (ImproperlyConfigured, MoySkladError, ValidationError, ObjectDoesNotExist, IntegrityError) as error:
            form.add_error(None, action_error(error))
        else:
            messages.success(request, "Карточка создана без публикации." if operation == "create" else "Связь сохранена; проверка цены поставлена в очередь. Публикация и блокировка изменяются отдельно.")
            return redirect("admin:catalog_product_change", product.pk)
    return local_response(request, "admin/moysklad/candidate_action.html", {"title": titles[operation], "form": form, "source": {"name": source.name, "id": source.external_id, "code": source.code, "unit": _source_unit_label(source, config)}, "remap": operation == "remap"}, status=400 if request.method == "POST" and form.errors else 200)


@require_http_methods(["GET", "POST"])
@csrf_protect
@owner_permission("integrations.link_moysklad_product")
def moysklad_unblock(request, product_id):
    if not request.user.has_perm("catalog.change_product"):
        raise PermissionDenied
    try:
        config = configured_target()
    except (ImproperlyConfigured, MoySkladError) as error:
        messages.error(request, action_error(error))
        return redirect("admin:moysklad_status")
    product = get_object_or_404(Product, pk=product_id, moysklad_product__account_id=config.account_id)
    form = ConfirmForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            catalog_sync.unblock_product(product_id=product.pk, actor=request.user)
        except (ImproperlyConfigured, MoySkladError, ValidationError, ObjectDoesNotExist, IntegrityError) as error:
            form.add_error(None, action_error(error))
        else:
            messages.success(request, "Блокировка снята после проверки локального источника. Признак публикации сохранён.")
            return redirect("admin:catalog_product_change", product.pk)
    return local_response(request, "admin/moysklad/unblock.html", {"title": "Снять блокировку товара", "product": product, "form": form}, status=400 if request.method == "POST" and form.errors else 200)


@require_http_methods(["GET", "POST"])
@csrf_protect
@owner_permission("integrations.repair_moysklad_order")
def moysklad_order_action(request, link_id, operation):
    if operation not in ("retry", "link", "override", "resolve") or not request.user.has_perm("orders.view_order"):
        raise PermissionDenied
    try:
        config = configured_target()
    except (ImproperlyConfigured, MoySkladError) as error:
        messages.error(request, action_error(error))
        return redirect("admin:moysklad_status")
    link = get_object_or_404(MoySkladOrderLink.objects.select_related("order"), pk=link_id, account_id=config.account_id, environment=config.environment)
    data = request.POST if request.method == "POST" else None
    form = ExistingOrderForm(data) if operation == "link" else OrderRepairForm(data)
    positions = None
    if operation == "override":
        if link.attempted or link.external_id:
            messages.error(request, "Сначала установите результат предыдущей передачи; исправление связей сейчас недоступно.")
            return redirect("admin:orders_order_change", link.order_id)
        items = list(link.order.items.order_by("pk"))
        sources = MoySkladProduct.objects.filter(account_id=config.account_id, entity_type="product", archived=False, deleted_at__isnull=True, is_deferred=False, last_error_code="", unit_id=config.unit_id, last_checked_price__gt=0).order_by("name", "pk")
        positions = PositionOverrideFormSet(data, initial=[{"item_id": item.pk} for item in items], form_kwargs={"sources": sources})
        for position, item in zip(positions.forms, items):
            position.fields["source"].label = f"{item.product_name_snapshot} (позиция {item.pk})"
    if request.method == "POST" and form.is_valid() and (positions is None or positions.is_valid()):
        try:
            if operation == "retry":
                order_export.request_order_retry(link.pk, actor=request.user, reason=form.cleaned_data["reason"])
            elif operation == "link":
                order_export.request_existing_order_link(link.pk, form.cleaned_data["external_id"], actor=request.user, reason=form.cleaned_data["reason"])
            elif operation == "resolve":
                order_export.request_export_resolution(link.pk, actor=request.user, reason=form.cleaned_data["reason"])
            else:
                mapping = {str(position.cleaned_data["item_id"]): {"account_id": str(config.account_id), "entity_type": "product", "external_id": str(position.cleaned_data["source"].external_id), "unit_id": str(position.cleaned_data["source"].unit_id)} for position in positions if position.cleaned_data.get("source")}
                if not mapping:
                    raise ValidationError("Выберите хотя бы одну исправленную связь.")
                order_export.set_export_override(link.pk, mapping, actor=request.user, reason=form.cleaned_data["reason"])
        except (ImproperlyConfigured, MoySkladError, ValidationError, ObjectDoesNotExist, IntegrityError) as error:
            form.add_error(None, action_error(error))
        else:
            message = ("Проверка результата передачи поставлена в очередь; результат появится в карточке заявки."
                       if operation == "resolve" else
                       "Задача сохранена. Исполнитель проверит данные перед передачей; результат появится в карточке заявки.")
            messages.success(request, message)
            return redirect("admin:orders_order_change", link.order_id)
    titles = {"retry": "Повторить передачу", "link": "Связать с существующим заказом", "override": "Исправить связи позиций для передачи", "resolve": "Проверить результат передачи"}
    return local_response(request, "admin/moysklad/order_action.html", {"title": titles[operation], "form": form, "positions": positions, "order": {"pk": link.order_id, "order_number": link.order.order_number}, "operation": operation}, status=400 if request.method == "POST" and (form.errors or positions and positions.errors) else 200)
