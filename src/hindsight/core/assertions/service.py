from collections.abc import Iterable
from dataclasses import dataclass

from hindsight.core.assertions.models import Assertion, AssertionDraft, TemporalLookup
from hindsight.core.assertions.repository import AssertionRepository


@dataclass(frozen=True, slots=True)
class TemporalSnapshot:
    current_truth: Assertion
    known_at_decision: Assertion


class TemporalAssertionService:
    def __init__(self, repository: AssertionRepository) -> None:
        self._repository = repository

    def ingest_versions(self, drafts: Iterable[AssertionDraft]) -> list[Assertion]:
        ordered = sorted(drafts, key=lambda draft: draft.recorded_at)
        return [self._repository.append(draft) for draft in ordered]

    def reconstruct(self, lookup: TemporalLookup) -> TemporalSnapshot:
        current_truth, known_at_decision = self._repository.temporal_snapshot(lookup)
        return TemporalSnapshot(
            current_truth=current_truth,
            known_at_decision=known_at_decision,
        )
