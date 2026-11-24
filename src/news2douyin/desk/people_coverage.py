"""Read-side extraction coverage of the exact, distinct focus article versions."""
from sqlmodel import select

from ..extraction.models import ExtractionAttempt, MentionReview, TextMention, VersionReading
from ..storage.models import ArticleVersion


def version_coverage_columns(version_id):
    # Indexed lookups for the bounded set of source versions. No revision
    # payloads or attempt histories are loaded when opening a people view.
    def job(column):
        return (select(column).where(VersionReading.version_id == version_id)
                .correlate(ArticleVersion).scalar_subquery())

    all_mentions = select(TextMention.id).where(TextMention.version_id == version_id)
    current = all_mentions.where(TextMention.article == ArticleVersion.article_key,
                                 TextMention.revision == ArticleVersion.revision)
    reviewable = (current.outerjoin(MentionReview, MentionReview.id == TextMention.id)
                  .where(MentionReview.decision.is_(None) | (MentionReview.decision != 'rejected')))
    failed = select(ExtractionAttempt.version_id).where(ExtractionAttempt.version_id == version_id)
    return (job(VersionReading.state).label('reading_state'),
            job(VersionReading.method).label('reading_method'),
            failed.correlate(ArticleVersion).exists().label('failure_record'),
            all_mentions.correlate(ArticleVersion).exists().label('any_mentions'),
            current.correlate(ArticleVersion).exists().label('current_mentions'),
            reviewable.correlate(ArticleVersion).exists().label('reviewable_mentions'))


def coverage_values(row):
    return {name: getattr(row, name) for name in ('reading_state', 'reading_method',
            'failure_record', 'any_mentions', 'current_mentions', 'reviewable_mentions')}


def can_check_for_repair(state, method, has_mentions, has_failures):
    """The write path additionally locks, rechecks identity, and inspects raw provenance."""
    return state in (None, 'baseline') and not (method or has_mentions or has_failures)


def extraction_summary(sources, checked, projected):
    versions = {source['version_id']: checked[source['article_key']] for source in sources}
    summary = dict(versions=len(versions), unqueued=0, queued=0, running=0, retrying=0,
                   failed=0, completed=0, metadata_only=0, evidence_only=0, unknown=0,
                   completed_empty=0, with_mentions=0, with_reviewable_mentions=0,
                   rejected_only_versions=0, repair_candidates=0,
                   statements_without_person=projected['limits']['statements_without_person'],
                   with_person_actions=len({row['version_id'] for row in projected['actions']}))
    for version in versions.values():
        state, method = version['reading_state'], version['reading_method']
        any_mentions, failure = version['any_mentions'], version['failure_record']
        summary['repair_candidates'] += can_check_for_repair(state, method, any_mentions, failure)
        summary['with_mentions'] += version['current_mentions']
        summary['with_reviewable_mentions'] += version['reviewable_mentions']
        summary['rejected_only_versions'] += version['current_mentions'] and not version['reviewable_mentions']
        if state is None:
            if failure:
                state_key = 'failed'
            elif version['current_mentions']:
                state_key = 'evidence_only'
            else:
                state_key = 'unknown' if any_mentions else 'unqueued'
        elif state in ('baseline', 'focus_baseline', 'model_queued'):
            state_key = 'queued'
        elif state in ('rule_running', 'model_running'):
            state_key = 'running'
        elif state in ('rule_wait', 'model_wait'):
            state_key = 'retrying'
        else:
            state_key = {'ready': 'completed', 'metadata_only': 'metadata_only', 'error': 'failed'}.get(state, 'unknown')
        summary[state_key] += 1
        summary['completed_empty'] += state == 'ready' and not any_mentions
    return summary
