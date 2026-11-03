"""Gather traceable evidence and open questions across a watch's research rounds."""
from sqlmodel import select
from ..storage.utils import loads
from ..research.models import ResearchClaim, ResearchQuestion, ResearchSource
from .models import WatchReview


def carry_history(session, case, watch):
    from ..research.service import add_source
    from ..extraction.outline import points
    outline = points(session,watch,case.cutoff)
    if outline:
        case.goal += '\n本专题的长期子问题（在报告中标明对应问题及仍缺证据）：\n' + '\n'.join(p.text[:200] for p in outline[:12])
    cases = select(WatchReview.case_key).where(WatchReview.watch == watch,
        WatchReview.case_key != case.case_id, WatchReview.created <= case.cutoff)
    gaps = session.exec(select(ResearchQuestion).where(ResearchQuestion.case_id.in_(cases),
        ResearchQuestion.status != 'answered', ResearchQuestion.updated_at <= case.cutoff)
        .order_by(ResearchQuestion.updated_at.desc()).limit(5)).all()
    if gaps:
        case.goal += '\n前轮留下的待查问题：\n' + '\n'.join(q.text[:240] for q in gaps)
    claims = session.exec(select(ResearchClaim).where(ResearchClaim.case_id.in_(cases),
        ResearchClaim.created_at <= case.cutoff).order_by(ResearchClaim.created_at.desc()).limit(30)).all()
    copied = set()
    for claim in claims:
        for ref in loads(claim.evidence_json, []):
            sid = ref.get('source_id')
            if not sid or sid in copied:
                continue
            source = session.get(ResearchSource, sid)
            if not source or source.case_id != claim.case_id or source.retrieved_at > case.cutoff:
                continue
            paragraphs = loads(source.paragraphs_json, [])
            if not paragraphs:
                continue
            provenance = loads(source.provenance_json, {})
            provenance.update(prior_case=source.case_id, prior_source=sid)
            add_source(session, case, title=source.title, url=source.url,
                text='\n'.join(p['text'] for p in paragraphs), kind=source.kind,
                published_at=source.published_at, provenance=provenance)
            copied.add(sid)
            if len(copied) == 8:
                return
