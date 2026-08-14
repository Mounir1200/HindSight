"""CockroachDB repositories that borrow one pooled connection per operation."""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any
from uuid import UUID

from hindsight.adapters.telecom.remediation import (
    RemediationReceipt,
    TelecomCaseSeed,
    TelecomCaseSnapshot,
    TelecomRemediationPlan,
)
from hindsight.core.agents.models import AgentRunRecord, ToolCallRecord
from hindsight.core.assertions.models import Assertion, AssertionDraft, TemporalLookup
from hindsight.core.assertions.repository import CockroachAssertionRepository
from hindsight.core.decisions.models import DecisionJournalEntry
from hindsight.core.decisions.repository import CockroachDecisionRepository
from hindsight.core.memory import (
    MemoryEmbeddingReceipt,
    MemoryEmbeddingSource,
    ProceduralMemoryLookup,
    ProceduralMemoryRetrieval,
    TextEmbedding,
)
from hindsight.infrastructure.agent_runs import (
    AgentRunAmbiguousCommitError,
    CockroachAgentRunRepository,
)
from hindsight.infrastructure.managed_mcp import (
    CockroachInvestigationContextStore,
    InvestigationContextSnapshot,
)
from hindsight.infrastructure.telecom_remediation import (
    CockroachTelecomRemediationRepository,
    RemediationAmbiguousCommitError,
)
from hindsight.infrastructure.vector_memory import (
    CockroachTelecomVectorMemoryStore,
    VectorMemoryAmbiguousCommitError,
)

ConnectionCheckout = Callable[[], AbstractContextManager[Any]]


class PooledAssertionRepository:
    def __init__(self, checkout: ConnectionCheckout, *, max_retries: int = 3) -> None:
        self._checkout = checkout
        self._max_retries = _validated_retries(max_retries)

    def append(self, draft: AssertionDraft) -> Assertion:
        with self._checkout() as connection:
            return self._repository(connection).append(draft)

    def current_truth(self, lookup: TemporalLookup) -> Assertion:
        with self._checkout() as connection:
            return self._repository(connection).current_truth(lookup)

    def known_at_decision(self, lookup: TemporalLookup) -> Assertion:
        with self._checkout() as connection:
            return self._repository(connection).known_at_decision(lookup)

    def temporal_snapshot(self, lookup: TemporalLookup) -> tuple[Assertion, Assertion]:
        with self._checkout() as connection:
            return self._repository(connection).temporal_snapshot(lookup)

    def history(self, assertion_key: str) -> list[Assertion]:
        with self._checkout() as connection:
            return self._repository(connection).history(assertion_key)

    def _repository(self, connection: Any) -> CockroachAssertionRepository:
        return CockroachAssertionRepository(connection, max_retries=self._max_retries)


class PooledDecisionRepository:
    def __init__(self, checkout: ConnectionCheckout, *, max_retries: int = 3) -> None:
        self._checkout = checkout
        self._max_retries = _validated_retries(max_retries)

    def append(self, entry: DecisionJournalEntry) -> DecisionJournalEntry:
        with self._checkout() as connection:
            return self._repository(connection).append(entry)

    def get(self, decision_id: UUID) -> DecisionJournalEntry:
        with self._checkout() as connection:
            return self._repository(connection).get(decision_id)

    def _repository(self, connection: Any) -> CockroachDecisionRepository:
        return CockroachDecisionRepository(connection, max_retries=self._max_retries)


class PooledTelecomRemediationRepository:
    def __init__(self, checkout: ConnectionCheckout, *, max_retries: int = 3) -> None:
        self._checkout = checkout
        self._max_retries = _validated_retries(max_retries)

    def seed_case(self, seed: TelecomCaseSeed) -> None:
        with self._checkout() as connection:
            self._repository(connection).seed_case(seed)

    def apply_remediation(self, plan: TelecomRemediationPlan) -> RemediationReceipt:
        return _with_fresh_ambiguous_recovery(
            self._checkout,
            self._repository,
            lambda repository: repository.apply_remediation(plan),
            RemediationAmbiguousCommitError,
        )

    def snapshot(self, dispute_id: UUID, memory_key: str) -> TelecomCaseSnapshot:
        with self._checkout() as connection:
            return self._repository(connection).snapshot(dispute_id, memory_key)

    def retrieve(self, lookup: ProceduralMemoryLookup) -> ProceduralMemoryRetrieval:
        with self._checkout() as connection:
            return self._repository(connection).retrieve(lookup)

    def _repository(self, connection: Any) -> CockroachTelecomRemediationRepository:
        return CockroachTelecomRemediationRepository(
            connection,
            max_retries=self._max_retries,
        )


class PooledAgentRunRepository:
    def __init__(self, checkout: ConnectionCheckout, *, max_retries: int = 3) -> None:
        self._checkout = checkout
        self._max_retries = _validated_retries(max_retries)

    def start_run(self, record: AgentRunRecord) -> AgentRunRecord:
        return _with_fresh_ambiguous_recovery(
            self._checkout,
            self._repository,
            lambda repository: repository.start_run(record),
            AgentRunAmbiguousCommitError,
        )

    def record_tool_call(self, record: ToolCallRecord) -> ToolCallRecord:
        return _with_fresh_ambiguous_recovery(
            self._checkout,
            self._repository,
            lambda repository: repository.record_tool_call(record),
            AgentRunAmbiguousCommitError,
        )

    def complete_run(
        self,
        run_id: UUID,
        *,
        output: dict[str, Any],
        usage: dict[str, int],
        stop_reason: str,
        completed_at: datetime,
    ) -> AgentRunRecord:
        return _with_fresh_ambiguous_recovery(
            self._checkout,
            self._repository,
            lambda repository: repository.complete_run(
                run_id,
                output=output,
                usage=usage,
                stop_reason=stop_reason,
                completed_at=completed_at,
            ),
            AgentRunAmbiguousCommitError,
        )

    def fail_run(
        self,
        run_id: UUID,
        *,
        error: dict[str, Any],
        usage: dict[str, int],
        completed_at: datetime,
        stop_reason: str | None = None,
    ) -> AgentRunRecord:
        return _with_fresh_ambiguous_recovery(
            self._checkout,
            self._repository,
            lambda repository: repository.fail_run(
                run_id,
                error=error,
                usage=usage,
                completed_at=completed_at,
                stop_reason=stop_reason,
            ),
            AgentRunAmbiguousCommitError,
        )

    def get(self, run_id: UUID) -> AgentRunRecord:
        with self._checkout() as connection:
            return self._repository(connection).get(run_id)

    def tool_calls(self, run_id: UUID) -> tuple[ToolCallRecord, ...]:
        with self._checkout() as connection:
            return self._repository(connection).tool_calls(run_id)

    def _repository(self, connection: Any) -> CockroachAgentRunRepository:
        return CockroachAgentRunRepository(
            connection,
            max_retries=self._max_retries,
        )


class PooledTelecomVectorMemoryStore:
    def __init__(self, checkout: ConnectionCheckout, *, max_retries: int = 3) -> None:
        self._checkout = checkout
        self._max_retries = _validated_retries(max_retries)

    def source(self, memory_id: UUID) -> MemoryEmbeddingSource:
        with self._checkout() as connection:
            return self._repository(connection).source(memory_id)

    def existing(
        self,
        memory_id: UUID,
        model_id: str,
    ) -> MemoryEmbeddingReceipt | None:
        with self._checkout() as connection:
            return self._repository(connection).existing(memory_id, model_id)

    def store(
        self,
        source: MemoryEmbeddingSource,
        embedding: TextEmbedding,
        content_sha256: str,
        embedded_at: datetime,
    ) -> MemoryEmbeddingReceipt:
        return _with_fresh_ambiguous_recovery(
            self._checkout,
            self._repository,
            lambda repository: repository.store(
                source,
                embedding,
                content_sha256,
                embedded_at,
            ),
            VectorMemoryAmbiguousCommitError,
        )

    def retrieve(
        self,
        lookup: ProceduralMemoryLookup,
        embedding: TextEmbedding,
    ) -> ProceduralMemoryRetrieval:
        with self._checkout() as connection:
            return self._repository(connection).retrieve(lookup, embedding)

    def _repository(self, connection: Any) -> CockroachTelecomVectorMemoryStore:
        return CockroachTelecomVectorMemoryStore(
            connection,
            max_retries=self._max_retries,
        )


class PooledInvestigationContextStore:
    def __init__(self, checkout: ConnectionCheckout) -> None:
        self._checkout = checkout

    def persist(
        self,
        case_id: UUID,
        context: Mapping[str, object],
    ) -> InvestigationContextSnapshot:
        with self._checkout() as connection:
            return CockroachInvestigationContextStore(connection).persist(case_id, context)


def _validated_retries(max_retries: int) -> int:
    if max_retries < 0:
        raise ValueError("max_retries cannot be negative")
    return max_retries


def _with_fresh_ambiguous_recovery(
    checkout: ConnectionCheckout,
    repository_factory: Callable[[Any], Any],
    operation: Callable[[Any], Any],
    ambiguous_error: type[Exception],
) -> Any:
    """Replay once only after returning the connection with the unknown outcome."""

    try:
        with checkout() as connection:
            return operation(repository_factory(connection))
    except ambiguous_error:
        with checkout() as connection:
            return operation(repository_factory(connection))
