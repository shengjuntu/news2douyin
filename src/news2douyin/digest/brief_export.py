"""Export the published edition with any exact, already-completed reading translations."""
from sqlmodel import select

from ..storage.models import TaskRecord
from ..translations.daily import daily_card_descriptor, resource_key, source_material
from ..translations.models import ReadingTranslation
from ..translations.service import translation_identity


def _completed_translations(session, issue):

    ids = {}
    for card in issue['items']:
        descriptor = daily_card_descriptor(issue['day'], card)
        if not descriptor or descriptor['already_chinese']:
            continue
        key = descriptor['key']
        resource = resource_key(issue['day'], key, descriptor['fingerprint'])
        original, language, _, scope, revision = source_material(card, descriptor)
        ident, _ = translation_identity('daily_card', resource, revision, scope, 'zh-CN', original, language)
        ids[ident] = key

    translations = {}
    identifiers = list(ids)
    for pos in range(0, len(identifiers), 500):
        query = (select(ReadingTranslation.id, ReadingTranslation.translated_text)
                 .join(TaskRecord, TaskRecord.task_id == ReadingTranslation.task_id)
                 .where(ReadingTranslation.id.in_(identifiers[pos:pos + 500]), TaskRecord.status == 'succeeded'))
        translations.update((ids[ident], text) for ident, text in session.exec(query) if text)
    return translations


def markdown_for_issue(session, issue):
    from .brief import render_brief

    return render_brief(issue, translations=_completed_translations(session, issue))


def markdown_for_shortlist(session, issue):
    """Export every published card using batched, read-only translation lookups."""
    from .shortlist import render_shortlist

    return render_shortlist(issue, translations=_completed_translations(session, issue))
