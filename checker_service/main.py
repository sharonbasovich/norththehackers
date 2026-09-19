from __future__ import annotations

import hmac
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.services.lean_checker import LeanChecker

PROJECT_DIR = Path(os.environ.get("MATHLAB_LEAN_PROJECT_DIR", "/srv/lean"))
CHECKER_TOKEN = os.environ.get("MATHLAB_LEAN_CHECKER_TOKEN", "")
TIMEOUT_SECONDS = int(os.environ.get("MATHLAB_LEAN_TIMEOUT_SECONDS", "300"))
DEFAULT_ALLOWED_AXIOMS = ("propext", "Classical.choice", "Quot.sound")

app = FastAPI(title="MathLab isolated Lean checker", docs_url=None, redoc_url=None)


def require_token(x_checker_token: str | None = Header(default=None)) -> None:
    if not CHECKER_TOKEN:
        raise HTTPException(503, "checker token is not configured")
    if x_checker_token is None or not hmac.compare_digest(x_checker_token, CHECKER_TOKEN):
        raise HTTPException(403, "invalid checker token")


class CheckRequest(BaseModel):
    source: str = Field(max_length=200_000)
    target_decl: str = Field(max_length=500)
    approved_target: str = Field(default="", max_length=4000)


def checker() -> LeanChecker:
    # The isolated service owns this policy. Callers cannot widen the allow-list.
    return LeanChecker(PROJECT_DIR, DEFAULT_ALLOWED_AXIOMS, TIMEOUT_SECONDS)


@app.get("/health")
def health() -> dict:
    local = checker()
    return {"ok": True, "available": local.available(), "toolchain": local.toolchain()}


@app.get("/environment")
def environment(x_checker_token: str | None = Header(default=None)) -> dict:
    require_token(x_checker_token)
    return checker().environment()


@app.post("/check")
def check(body: CheckRequest, x_checker_token: str | None = Header(default=None)) -> dict:
    require_token(x_checker_token)
    result = checker().check(body.source, body.target_decl, body.approved_target)
    return result.as_details()
