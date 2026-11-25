"""Browser entry points for manually verified product references."""
import os
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Query
from fastapi.responses import HTMLResponse

from ..events.product_evidence import ProductID, for_product
from ..selection.source_content import source_url
from ..storage.db import session_scope
from .tasks import task_call


def trade_view_public_url():
    value = os.getenv('NEWS2DOUYIN_TRADE_VIEW_PUBLIC_URL', '').strip().rstrip('/')
    if not source_url(value) or '\\' in value:
        return ''
    parts = urlsplit(value)
    return '' if parts.query or parts.fragment else value


def register_product_pages(app, engine, render):
    @app.get('/commodities/{product_id}/events', response_class=HTMLResponse)
    def product_events(product_id: ProductID, before_id: Annotated[int | None, Query(ge=1)] = None):
        with session_scope(engine) as session:
            page = task_call(for_product, session, product_id, before_id=before_id)
        return render('product_events.html', active='radar', page=page, request_before=before_id,
                      trade_view_public_url=trade_view_public_url())
