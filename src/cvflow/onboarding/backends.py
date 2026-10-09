"""Generation backends: turn a prompt into a validated CandidateBundle (or LaTeX repairs)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from cvflow.llm import LLMError
from cvflow.onboarding import OnboardingError
from cvflow.onboarding.bundle import CandidateBundle, CandidateSkills, ResumeBundle
from cvflow.onboarding.prompt import build_stage2_prompt

log = logging.getLogger(__name__)

_MAX_OUT = 16000  # a whole bundle is far larger than ChatClient's 512-token default


class _Client(Protocol):
    def generate(self, prompt: str, **kwargs: Any) -> str: ...

    def generate_structured(self, prompt: str, **kwargs: Any) -> str: ...


class GenerationBackend(Protocol):
    def generate_bundle(self, prompt: str) -> CandidateBundle: ...

    def repair(self, prompt: str) -> dict[str, str]: ...


class _Stage1(BaseModel):
    profile_docs: dict[str, str]
    project_docs: dict[str, str]
    form_fields: dict[str, str]
    resume: ResumeBundle
    review_notes: list[str]


class _Stage2(BaseModel):
    candidate_skills: CandidateSkills
    discovery: dict[str, Any]
    preferences: dict[str, Any]
    review_notes: list[str]


_T = TypeVar("_T", bound=BaseModel)


def _structured(client: _Client, prompt: str, model: type[_T]) -> _T:
    try:
        raw = client.generate_structured(prompt, schema=model, max_output_tokens=_MAX_OUT)
        return model.model_validate_json(raw)
    except (LLMError, ValidationError) as exc:
        raise OnboardingError(f"{model.__name__} generation failed: {exc}") from exc


def _parse_files(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise OnboardingError(f"repair reply is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise OnboardingError("repair reply must be a JSON object of filename -> string")
    return data


def _repair(client: _Client, prompt: str) -> dict[str, str]:
    try:
        raw = client.generate(prompt, json_object=True, max_tokens=_MAX_OUT)
    except LLMError as exc:
        raise OnboardingError(f"repair generation failed: {exc}") from exc
    return _parse_files(raw)


class SingleShotBackend:
    def __init__(self, client: _Client) -> None:
        self._client = client

    def generate_bundle(self, prompt: str) -> CandidateBundle:
        return _structured(self._client, prompt, CandidateBundle)

    def repair(self, prompt: str) -> dict[str, str]:
        return _repair(self._client, prompt)


class FrontierBackend(SingleShotBackend):
    """Single-shot generation over a bring-your-own frontier client."""


class TwoStageBackend:
    """Stage 1 writes the facts; stage 2 derives skills/config from stage 1's output."""

    def __init__(self, stage1_client: _Client, stage2_client: _Client) -> None:
        self._s1 = stage1_client
        self._s2 = stage2_client

    def generate_bundle(self, prompt: str) -> CandidateBundle:
        s1 = _structured(self._s1, prompt, _Stage1)
        s2 = _structured(self._s2, build_stage2_prompt(s1.model_dump_json(indent=1)), _Stage2)
        try:
            return CandidateBundle.model_validate(
                {
                    **s1.model_dump(),
                    "candidate_skills": s2.candidate_skills.model_dump(),
                    "discovery": s2.discovery,
                    "preferences": s2.preferences,
                    "review_notes": [*s1.review_notes, *s2.review_notes],
                }
            )
        except ValidationError as exc:
            raise OnboardingError(f"merged bundle invalid: {exc}") from exc

    def repair(self, prompt: str) -> dict[str, str]:
        return _repair(self._s1, prompt)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class ManualBackend:
    """Human-in-the-loop: show the prompt, read back a pasted JSON reply."""

    def __init__(self, prompt_out: Callable[[str], None], reply_in: Callable[[], str]) -> None:
        self._out = prompt_out
        self._in = reply_in

    def _ask(self, prompt: str) -> str:
        self._out(prompt)
        text = self._in().strip()
        m = _FENCE_RE.search(text)
        return m.group(1).strip() if m else text

    def generate_bundle(self, prompt: str) -> CandidateBundle:
        try:
            return CandidateBundle.model_validate_json(self._ask(prompt))
        except ValidationError as exc:
            raise OnboardingError(f"pasted reply is not a valid bundle: {exc}") from exc

    def repair(self, prompt: str) -> dict[str, str]:
        return _parse_files(self._ask(prompt))


class BackendChain:
    def __init__(self, backends: list[GenerationBackend]) -> None:
        self._backends = backends

    def generate_bundle(self, prompt: str) -> CandidateBundle:
        failures: list[str] = []
        for b in self._backends:
            name = type(b).__name__
            try:
                return b.generate_bundle(prompt)
            except (OnboardingError, LLMError) as exc:
                log.warning("onboarding backend %s failed: %s", name, exc)
                failures.append(f"{name}: {exc}")
        raise OnboardingError("all generation backends failed: " + "; ".join(failures))

    def repair(self, prompt: str) -> dict[str, str]:
        if not self._backends:
            raise OnboardingError("no generation backends configured")
        return self._backends[0].repair(prompt)


def auto_chain(
    onboarding_cfg: Any,
    tailoring_client: _Client,
    distillation_client: _Client,
    frontier_client: _Client | None = None,
) -> BackendChain:
    chain: list[GenerationBackend] = []
    if frontier_client is not None:
        chain.append(FrontierBackend(frontier_client))
    chain.append(TwoStageBackend(tailoring_client, distillation_client))
    return BackendChain(chain)


def manual_chain(prompt_out: Callable[[str], None], reply_in: Callable[[], str]) -> BackendChain:
    return BackendChain([ManualBackend(prompt_out, reply_in)])
