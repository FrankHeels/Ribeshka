"""Archive and resolve the consent text used by public request forms."""

import hashlib

from django.core.exceptions import ImproperlyConfigured

from .legal_documents import read_legal_document_source
from .models import ConsentRevision


def get_consent_revision(revision_id, *, using="default"):
    """Return an archived revision by its signed identifier, if it exists."""
    return ConsentRevision.objects.using(using).filter(revision_id=revision_id).first()


def get_current_consent_revision(*, using="default"):
    """Archive the current source consent once and return its immutable row.

    The repository Markdown file remains the only source for new revisions.
    Identical source bytes reuse the existing archive row; changed bytes create
    a separate row and never rewrite an earlier revision.
    """
    source = read_legal_document_source("consent")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    revision, _created = ConsentRevision.objects.using(using).get_or_create(
        sha256=digest,
        defaults={"text": source},
    )
    if (
        revision.text != source
        or revision.sha256 != digest
        or revision.revision_id != f"sha256:{digest}"
    ):
        raise ImproperlyConfigured("Архив редакции согласия не совпадает с исходным Markdown.")
    return revision
