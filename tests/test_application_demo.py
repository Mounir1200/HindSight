from contextlib import contextmanager

import hindsight.application.demo as demo_module
from hindsight.infrastructure.pooled_repositories import (
    PooledAgentRunRepository,
    PooledAssertionRepository,
    PooledDecisionRepository,
    PooledTelecomRemediationRepository,
)


def test_database_demo_builds_short_checkout_repositories_without_reserving_a_connection(
    monkeypatch,
) -> None:
    active_checkouts = 0
    checkout_calls = 0

    @contextmanager
    def checkout():
        nonlocal active_checkouts, checkout_calls
        checkout_calls += 1
        active_checkouts += 1
        try:
            yield object()
        finally:
            active_checkouts -= 1

    def run_demo_workflow(
        assertion_repository,
        decision_repository,
        remediation_repository,
        backend,
        **options,
    ):
        assert active_checkouts == 0
        assert isinstance(assertion_repository, PooledAssertionRepository)
        assert isinstance(decision_repository, PooledDecisionRepository)
        assert isinstance(remediation_repository, PooledTelecomRemediationRepository)
        assert isinstance(options["agent_run_repository"], PooledAgentRunRepository)
        assert backend == "cockroachdb"
        return {"status": "ok"}

    monkeypatch.setattr(demo_module, "run_demo_workflow", run_demo_workflow)

    payload = demo_module.execute_demo(
        "postgresql://runtime",
        connection_context_factory=checkout,
    )

    assert payload == {"status": "ok"}
    assert checkout_calls == 0
    assert active_checkouts == 0
