"""Load and render the fixed legal documents published with the site."""

from html import escape
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import markdown
from django.urls import reverse
from django.utils.safestring import mark_safe


LEGAL_DOCUMENTS = {
    "policy": {
        "filename": "personal-data-policy.md",
        "route": "content:privacy",
        "title": "Политика обработки персональных данных",
        "description": "Правила обработки персональных данных на сайте «Калининградская рыбёшка».",
        "draft_notice": (
            "Проект документа: поля в фигурных скобках не заполнены; реквизиты и условия обработки "
            "требуют уточнения перед применением."
        ),
    },
    "consent": {
        "filename": "personal-data-consent.md",
        "route": "content:personal_data_consent",
        "title": "Согласие на обработку персональных данных",
        "description": "Текст согласия на обработку данных для рассмотрения розничной заявки или оптового запроса.",
        "draft_notice": (
            "Проект документа: поля в фигурных скобках не заполнены; реквизиты и условия обработки "
            "требуют уточнения перед применением."
        ),
    },
    "metrica": {
        "filename": "yandex-metrica-consent.md",
        "route": "content:yandex_metrica_consent",
        "title": "Согласие на обработку персональных данных с помощью Яндекс Метрики",
        "description": "Проект отдельного согласия на будущую обработку данных с помощью Яндекс Метрики.",
        "draft_notice": (
            "Проект для будущего подключения: Яндекс Метрика на сайте пока не используется. "
            "Поля в фигурных скобках и настройки счётчика нужно проверить до его запуска."
        ),
    },
}

_DOCUMENTS_DIRECTORY = Path(__file__).resolve().parent.parent / "docs" / "legal"
_DOCUMENTS_BY_FILENAME = {
    document["filename"]: document["route"] for document in LEGAL_DOCUMENTS.values()
}
_DOCUMENTS_BY_PATH = {
    "privacy": "content:privacy",
    "personal-data-consent": "content:personal_data_consent",
    "yandex-metrica-consent": "content:yandex_metrica_consent",
}
_MARKDOWN_LINK_TARGET = re.compile(r"(?<=\]\()(?P<target>[^)\s]+)")
_LEADING_TITLE = re.compile(r"\A(?:\ufeff)?#\s+[^\r\n]*(?:\r?\n)+")
_TABLE_ELEMENT = re.compile(r"<table>.*?</table>", re.DOTALL)
_TABLE_HEADER = re.compile(r"<th(?=[\s>])")


def _document_route(target):
    parsed = urlsplit(target)
    path = unquote(parsed.path).rstrip("/")
    filename = Path(path).name
    route = _DOCUMENTS_BY_FILENAME.get(filename)
    if route:
        return route, parsed.fragment

    route = _DOCUMENTS_BY_PATH.get(filename)
    if route and (not parsed.query):
        return route, parsed.fragment
    return None, ""


def _rewrite_document_links(source):
    def replace(match):
        target = match.group("target")
        route, fragment = _document_route(target)
        if not route:
            return target
        href = reverse(route)
        if fragment:
            href = f"{href}#{fragment}"
        return href

    return _MARKDOWN_LINK_TARGET.sub(replace, source)


def read_legal_document_source(key):
    """Read the exact UTF-8 source for one allowlisted legal document."""
    document = LEGAL_DOCUMENTS[key]
    source_path = _DOCUMENTS_DIRECTORY / document["filename"]
    return source_path.read_bytes().decode("utf-8")


def render_legal_document(key, *, source=None):
    """Return display metadata and safe HTML for one allowlisted Markdown source."""
    document = LEGAL_DOCUMENTS[key]
    if source is None:
        source = read_legal_document_source(key)
    source = _LEADING_TITLE.sub("", source, count=1)
    source = _rewrite_document_links(source)

    # These files are trusted, repository-managed content, but Markdown's raw
    # HTML support is unnecessary for them. Escaping it keeps the renderer
    # safe if an HTML fragment is added to a document later.
    rendered = markdown.markdown(escape(source, quote=False), extensions=["tables"])
    rendered = _TABLE_HEADER.sub('<th scope="col"', rendered)
    rendered = _TABLE_ELEMENT.sub(
        '<div class="legal-table-wrap" role="region" tabindex="0" '
        'aria-label="Таблица документа, прокручивается по горизонтали">\g<0></div>',
        rendered,
    )

    return {
        "title": document["title"],
        "description": document["description"],
        "draft_notice": document["draft_notice"],
        "html": mark_safe(rendered),
    }
