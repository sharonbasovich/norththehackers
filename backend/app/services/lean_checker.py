"""Independent Lean 4 checking under the strict initial policy (docs/plan.md §6.5).

A proof artifact is only `verified` when:
  * the whole file compiles with the pinned toolchain in the lab's Lean project,
  * the target declaration exists and its statement matches the approved target exactly
    (after whitespace normalization),
  * `#print axioms` reports only allowed axioms (no `sorryAx`, no user axioms),
  * the source contains no escape hatches (`axiom`, `unsafe`, `implemented_by`, `extern`,
    `native_decide`, `set_option` overrides).

Anything else is `rejected`, and if Lean is not installed the result is `checker_unavailable`.
Workers never call this to certify themselves; the scheduler does.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

FORBIDDEN_PATTERNS = (
    r"^\s*axiom\b",
    r"\bunsafe\b",
    r"implemented_by",
    r"@\[extern",
    r"native_decide",
    r"^\s*set_option\b",
    r"\bsorry\b",
)

DECL_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)*"
    r"(theorem|lemma)\s+([A-Za-z_][\w.']*)\s*(.*?)\s*:=",
    re.S | re.M,
)
AXIOMS_RE = re.compile(r"'([^']+)' depends on axioms: \[([^\]]*)\]")
NO_AXIOMS_RE = re.compile(r"'([^']+)' does not depend on any axioms")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


@dataclass
class LeanCheckResult:
    status: str  # verified | rejected | checker_unavailable
    reasons: list[str] = field(default_factory=list)
    axioms: list[str] = field(default_factory=list)
    toolchain: str = ""
    log: str = ""
    declaration: str = ""
    content_hash: str = ""

    def as_details(self) -> dict:
        return {
            "status": self.status,
            "reasons": self.reasons,
            "axioms": self.axioms,
            "toolchain": self.toolchain,
            "declaration": self.declaration,
            "content_hash": self.content_hash,
            "log_tail": self.log[-4000:],
        }


class LeanChecker:
    def __init__(
        self,
        project_dir: Path | None,
        allowed_axioms: tuple[str, ...] = ("propext", "Classical.choice", "Quot.sound"),
        timeout_seconds: int = 300,
    ):
        self.project_dir = project_dir
        self.allowed_axioms = set(allowed_axioms)
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return (
            self.project_dir is not None
            and (self.project_dir / "lakefile.toml").exists()
            and shutil.which("lake") is not None
        )

    def toolchain(self) -> str:
        try:
            out = subprocess.run(
                ["lake", "env", "lean", "--version"],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            return out.stdout.strip() or out.stderr.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"unknown ({exc})"

    def environment(self) -> dict:
        if self.project_dir is None or not self.available():
            return {"available": False}

        def read(path: Path) -> str:
            return path.read_text() if path.exists() else ""

        return {
            "available": True,
            "toolchain": read(self.project_dir / "lean-toolchain").strip(),
            "lakefile": read(self.project_dir / "lakefile.toml"),
            "MathLab/Basic.lean": read(self.project_dir / "MathLab" / "Basic.lean"),
            "allowed_axioms": sorted(self.allowed_axioms),
        }

    def static_reject_reasons(
        self, source: str, target_decl: str, approved_target: str
    ) -> list[str]:
        reasons = []
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, source, re.M):
                reasons.append(f"forbidden construct matches /{pattern}/")
        decls = {name: (kind, sig) for kind, name, sig in DECL_RE.findall(source)}
        if target_decl not in decls:
            reasons.append(f"target declaration {target_decl!r} not found")
        elif approved_target and normalize(decls[target_decl][1]) != normalize(approved_target):
            reasons.append(
                "target statement differs from approved target: "
                f"{normalize(decls[target_decl][1])!r} != {normalize(approved_target)!r}"
            )
        return reasons

    def check(self, source: str, target_decl: str, approved_target: str = "") -> LeanCheckResult:
        content_hash = hashlib.sha256(source.encode()).hexdigest()
        result = LeanCheckResult(
            status="rejected", content_hash=content_hash, declaration=target_decl
        )
        result.reasons = self.static_reject_reasons(source, target_decl, approved_target)
        if result.reasons:
            return result
        if not self.available():
            result.status = "checker_unavailable"
            result.reasons = ["Lean toolchain or lab project not available"]
            return result
        assert self.project_dir is not None
        result.toolchain = self.toolchain()
        check_dir = self.project_dir / ".checks"
        check_dir.mkdir(exist_ok=True)
        path = check_dir / f"Check_{content_hash[:16]}.lean"
        path.write_text(source.rstrip() + f"\n\n#print axioms {target_decl}\n")
        try:
            proc = subprocess.run(
                ["lake", "env", "lean", str(path)],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result.reasons = [f"lean timed out after {self.timeout_seconds}s"]
            return result
        finally:
            path.unlink(missing_ok=True)
        result.log = proc.stdout + proc.stderr
        if proc.returncode != 0 or re.search(r"error:", result.log):
            result.reasons = ["lean reported errors"]
            return result
        match = AXIOMS_RE.search(result.log)
        if match:
            result.axioms = [a.strip() for a in match.group(2).split(",") if a.strip()]
        elif not NO_AXIOMS_RE.search(result.log):
            result.reasons = ["could not read #print axioms output"]
            return result
        disallowed = [a for a in result.axioms if a not in self.allowed_axioms]
        if disallowed:
            result.reasons = [f"disallowed axioms: {disallowed}"]
            return result
        result.status = "verified"
        return result


class RemoteLeanChecker(LeanChecker):
    """Client for the isolated checker container.

    Static policy checks run on both sides. Network or checker failures become an explicit
    `checker_unavailable` result rather than accidentally accepting or crashing ingestion.
    """

    def __init__(
        self,
        service_url: str,
        *,
        token: str,
        allowed_axioms: tuple[str, ...] = ("propext", "Classical.choice", "Quot.sound"),
        timeout_seconds: int = 300,
    ):
        super().__init__(None, allowed_axioms, timeout_seconds)
        self.service_url = service_url.rstrip("/")
        self.token = token

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Checker-Token": self.token}

    def available(self) -> bool:
        try:
            response = httpx.get(f"{self.service_url}/health", timeout=5)
            response.raise_for_status()
            return bool(response.json().get("available"))
        except (httpx.HTTPError, ValueError):
            return False

    def environment(self) -> dict:
        try:
            response = httpx.get(
                f"{self.service_url}/environment", headers=self._headers, timeout=10
            )
            response.raise_for_status()
            return dict(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            return {"available": False, "reason": f"isolated checker unavailable: {exc}"}

    def check(self, source: str, target_decl: str, approved_target: str = "") -> LeanCheckResult:
        content_hash = hashlib.sha256(source.encode()).hexdigest()
        reasons = self.static_reject_reasons(source, target_decl, approved_target)
        if reasons:
            return LeanCheckResult(
                status="rejected",
                reasons=reasons,
                declaration=target_decl,
                content_hash=content_hash,
            )
        try:
            response = httpx.post(
                f"{self.service_url}/check",
                headers=self._headers,
                json={
                    "source": source,
                    "target_decl": target_decl,
                    "approved_target": approved_target,
                },
                timeout=self.timeout_seconds + 10,
            )
            response.raise_for_status()
            payload = response.json()
            return LeanCheckResult(
                status=str(payload["status"]),
                reasons=list(payload.get("reasons", [])),
                axioms=list(payload.get("axioms", [])),
                toolchain=str(payload.get("toolchain", "")),
                log=str(payload.get("log_tail", "")),
                declaration=str(payload.get("declaration", target_decl)),
                content_hash=str(payload.get("content_hash", content_hash)),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            return LeanCheckResult(
                status="checker_unavailable",
                reasons=[f"isolated checker unavailable: {exc}"],
                declaration=target_decl,
                content_hash=content_hash,
            )
