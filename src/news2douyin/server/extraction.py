from pathlib import Path
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select
import yaml
from ..storage.models import ArticleVersion
from ..extraction import service, outline
from ..extraction.schema import Candidate
from .research import same_origin
from .tasks import task_call


class Decision(BaseModel):
    decision: str


class Point(BaseModel):
    parent: str = Field(default='',max_length=128)
    text: str = Field(min_length=1,max_length=400)


def register(app, db):
    def version(s, article, revision):
        found = s.exec(select(ArticleVersion).where(ArticleVersion.article_key==article,
            ArticleVersion.revision==revision)).first()
        if not found:
            raise HTTPException(404,'报道版本不存在')
        return found

    @app.get('/api/desk/graph-schema')
    def schema():
        path = Path(__file__).parents[1]/'extraction'/'graph-schema.yaml'
        return dict(graph=yaml.safe_load(path.read_text(encoding='utf-8')),candidate=Candidate.model_json_schema())

    @app.get('/api/desk/articles/{article}/{revision}/mentions')
    def read(article:str, revision:int, s=Depends(db)):
        return service.article_view(s,version(s,article,revision))

    @app.post('/api/desk/articles/{article}/{revision}/model',dependencies=[Depends(same_origin)])
    def model(article:str,revision:int,s=Depends(db)):
        return {'state':task_call(service.schedule_model,s,version(s,article,revision))}

    @app.post('/api/desk/mentions/{key}/review',dependencies=[Depends(same_origin)])
    def review(key:str,p:Decision,s=Depends(db)):
        task_call(service.decide,s,key,p.decision)
        return {'ok':True}

    @app.get('/api/desk/mentions/{key}/candidates')
    def peers(key:str,s=Depends(db)):
        return {'items':task_call(service.neighbors,s,key)}

    @app.post('/api/focus/{key}/outline',dependencies=[Depends(same_origin)])
    def add(key:str,p:Point,s=Depends(db)):
        return {'key':task_call(outline.add,s,key,p.parent,p.text)}

    @app.post('/api/focus/{key}/outline/{point}/review',dependencies=[Depends(same_origin)])
    def point_review(key:str,point:str,s=Depends(db)):
        return {'ticket':task_call(outline.request_review,s,key,point)}
