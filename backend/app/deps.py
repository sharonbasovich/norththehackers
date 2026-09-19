from functools import lru_cache

from .config import get_settings
from .services.devin_client import build_client
from .services.ingest import Ingestor
from .services.lean_checker import LeanChecker, RemoteLeanChecker
from .services.publication import Publisher
from .services.scheduler import Scheduler


@lru_cache
def get_scheduler() -> Scheduler:
    settings = get_settings()
    checker: LeanChecker
    if settings.lean_checker_url:
        checker = RemoteLeanChecker(
            settings.lean_checker_url,
            token=settings.lean_checker_token,
            allowed_axioms=settings.allowed_axioms,
            timeout_seconds=settings.lean_timeout_seconds,
        )
    else:
        checker = LeanChecker(
            settings.lean_project_dir if str(settings.lean_project_dir) not in ("", ".") else None,
            allowed_axioms=settings.allowed_axioms,
            timeout_seconds=settings.lean_timeout_seconds,
        )
    ingestor = Ingestor(settings.artifact_dir, checker)
    return Scheduler(settings, build_client(settings), ingestor, Publisher())
