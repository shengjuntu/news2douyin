"""Historical snapshots and replacement of a program node's published results."""
from urllib.parse import urlencode

from sqlalchemy import delete, func
from sqlmodel import select

from ..storage.utils import dumps, loads
from .models import (CausalAgentNote, CausalFocus, CausalJob, CausalNode, CausalProgramRetrieval,
                     CausalProgramRevision, CausalProgramStep, CausalRetiredVisit)


def latest_revisions(session, case_key):
    return dict(session.exec(select(CausalProgramRevision.node_key, func.max(CausalProgramRevision.id))
                .where(CausalProgramRevision.case_key == case_key).group_by(CausalProgramRevision.node_key)).all())


def snapshot(session, case_key, node_key, nodes):
    # The live view is ordered by depth. Include only this parent and its descendants.
    included = {node_key}
    branch = []
    for item in nodes:
        if item['key'] == node_key or item['parent_key'] in included:
            branch.append(item)
            included.add(item['key'])
    session.add(CausalProgramRevision(case_key=case_key, node_key=node_key, nodes_json=dumps(branch)))


def read(session, case_key, revision_id):
    revision = session.get(CausalProgramRevision, revision_id)
    if revision is None or revision.case_key != case_key:
        raise KeyError('未找到该调查的历史版本')
    nodes = loads(revision.nodes_json, [])
    focus = session.get(CausalFocus, case_key)
    path = ('/graphs?' + urlencode({'view':'focus', 'focus':focus.focus_key, 'investigation':case_key})
            if focus else '/radar/causes?' + urlencode({'investigation':case_key}))
    return dict(id=revision.id, case_key=case_key, node_key=revision.node_key, current_path=path,
                saved_at=revision.saved_at, nodes=nodes)


def retire(session, case_key, keys):
    if not keys:
        return
    removed = set(keys)
    nodes = session.exec(select(CausalNode.key, CausalNode.parent_key)
                         .where(CausalNode.case_key == case_key).order_by(CausalNode.depth))
    for key, parent in nodes:
        if parent in removed:
            removed.add(key)
    visits = session.exec(select(CausalNode).join(CausalJob, CausalJob.node_key == CausalNode.key)
                          .where(CausalNode.key.in_(removed))).all()
    for node in visits:
        session.add(CausalRetiredVisit(case_key=case_key, node_key=node.key, ref=node.ref,
                                      depth=node.depth, beam_selected=node.beam_selected))
    for model in (CausalJob, CausalAgentNote, CausalProgramRetrieval, CausalProgramStep):
        session.execute(delete(model).where(model.node_key.in_(removed)))
    session.execute(delete(CausalNode).where(CausalNode.key.in_(removed)))
