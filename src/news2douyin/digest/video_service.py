"""Read previously saved video candidate editions."""
from datetime import date

from sqlmodel import Session

from . import service
from .models import VideoDigestIssue


def read_issue(session: Session, day: date) -> dict:
    return service.read_issue(session, day, model=VideoDigestIssue)
