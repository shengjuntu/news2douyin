from fastapi import Response
from sqlmodel import Session

from ..events import workbench as work, research
from ..events.schemas import (VersionPayload,EventCreate,EventEdit,SourceEdit,MomentEdit,
                              MergeEdit,SplitEdit,RelationEdit,ResearchQuery,ResearchImport)
from ..storage.utils import dumps
from .tasks import task_call


def register_event_routes(app,engine):
    def call(fn,*args):
        with Session(engine) as session:
            return task_call(fn,session,*args)

    @app.post('/api/events')
    def create(payload:EventCreate): return call(work.create_event,payload.model_dump())

    @app.get('/api/events/{key}/workspace')
    def workspace(key:str): return call(work.workspace,key)

    @app.put('/api/events/{key}/workspace')
    def edit(key:str,payload:EventEdit): return call(work.edit_event,key,payload.model_dump())

    @app.post('/api/events/{key}/sources')
    def add(key:str,payload:SourceEdit): return call(work.add_sources,key,payload.model_dump())

    @app.delete('/api/events/{key}/sources/{article_key}')
    def remove(key:str,article_key:str,payload:VersionPayload): return call(work.remove_source,key,article_key,payload.expected_version)

    @app.post('/api/events/{key}/moments')
    def add_moment(key:str,payload:MomentEdit): return call(work.save_moment,key,payload.model_dump())

    @app.put('/api/events/{key}/moments/{moment_key}')
    def edit_moment(key:str,moment_key:str,payload:MomentEdit): return call(work.save_moment,key,payload.model_dump(),moment_key)

    @app.delete('/api/events/{key}/moments/{moment_key}')
    def delete_moment(key:str,moment_key:str,payload:VersionPayload): return call(work.delete_moment,key,moment_key,payload.expected_version)

    @app.post('/api/events/{key}/merge')
    def merge(key:str,payload:MergeEdit): return call(work.merge,key,payload.model_dump())

    @app.post('/api/events/{key}/split')
    def split(key:str,payload:SplitEdit): return call(work.split,key,payload.model_dump())

    @app.post('/api/events/{key}/relations')
    def relate(key:str,payload:RelationEdit): return call(work.relate,key,payload.model_dump())

    @app.delete('/api/events/{key}/relations/{relation_key}')
    def unrelate(key:str,relation_key:str,payload:VersionPayload): return call(work.unrelate,key,relation_key,payload.expected_version)

    @app.post('/api/events/{key}/research')
    def search(key:str,payload:ResearchQuery): return task_call(research.search,engine,key,payload.model_dump())

    @app.get('/api/events/{key}/research/{research_key}')
    def get_research(key:str,research_key:str): return call(research.result,key,research_key)

    @app.post('/api/events/{key}/research/{research_key}/import')
    def import_research(key:str,research_key:str,payload:ResearchImport): return call(research.import_results,key,research_key,payload.model_dump())

    @app.get('/api/events/{key}/evidence-export')
    def export(key:str):
        data=call(work.export_evidence,key)
        return Response(dumps(data),media_type='application/json',headers={'Content-Disposition':'attachment; filename="event-evidence.json"'})
