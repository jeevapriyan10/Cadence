"""EchoStore-backed execution run store for Cadence API sessions."""

from typing import Any, Optional
import uuid

from cadence.domain.db import create_all_tables, get_session
from cadence.domain.models import DecisionRecord, NetworkRun
from cadence.domain.schemas import ScheduledBlockSchema
from cadence.echo.store import echo_store
from cadence.generator import generate_synthetic_network
from cadence.solve.solver import SolveResult


class RunStore:
    """Persistent execution run store backed by EchoStore (SQLAlchemy database).

    Acts as an orchestration bridge for the Cadence API layer, persisting NetworkRuns
    and DecisionRecords to the database while maintaining in-memory transient caches
    for solve contexts, ripple reports, and post-hoc explanations.
    """

    def __init__(self) -> None:
        self._network_cache: dict[str, dict[str, Any]] = {}
        self._solve_cache: dict[str, Any] = {}
        self._context_cache: dict[str, Any] = {}
        self._ripple_cache: dict[str, Any] = {}
        self._explain_cache: dict[str, Any] = {}
        # Ensure database tables exist upon store initialization
        try:
            create_all_tables()
        except Exception:
            pass

    def create_run(
        self,
        network_data: Optional[dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> str:
        """Persist a new network run record and initialize session cache.

        Args:
            network_data: Dictionary containing sections, adjacencies, train_slots,
                maintenance_tasks, profile_name, seed.
            run_id: Optional preset UUID; if None, a new UUID4 string is generated.

        Returns:
            str: Unique run_id string.
        """
        profile_name = "metro"
        seed = 42
        section_count = 0
        train_count = 0
        task_count = 0

        if network_data is not None:
            profile_name = network_data.get("profile_name", "metro")
            seed = network_data.get("seed", 42)
            section_count = len(network_data.get("sections", []))
            train_count = len(network_data.get("train_slots", []))
            task_count = len(network_data.get("maintenance_tasks", []))

        with get_session() as session:
            net_run = echo_store.record_network_run(
                session=session,
                profile_name=profile_name,
                seed=seed,
                section_count=section_count,
                train_count=train_count,
                task_count=task_count,
                run_id=run_id,
            )
            allocated_id = net_run.id

        if network_data is not None:
            self._network_cache[allocated_id] = network_data

        return allocated_id

    def set_network_cache(self, run_id: str, network_data: dict[str, Any]) -> None:
        """Cache network dataset in memory for the given run_id."""
        self._network_cache[run_id] = network_data

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """Retrieve run data for a given run_id from persistence and cache.

        Args:
            run_id: Unique run identifier.

        Returns:
            Optional[dict[str, Any]]: Run dictionary if found, None otherwise.
        """
        with get_session() as session:
            net_run = session.get(NetworkRun, run_id)
            if net_run is None:
                return None
            latest_dec = echo_store.get_latest_decision(session, run_id)

        network = self._network_cache.get(run_id)
        if network is None and net_run.section_count > 0:
            try:
                network = generate_synthetic_network(
                    profile_name=net_run.profile_name,
                    seed=net_run.seed,
                    section_count=net_run.section_count,
                    train_count=net_run.train_count,
                    task_count=net_run.task_count,
                )
                self._network_cache[run_id] = network
            except Exception:
                network = None

        solve_result = self._solve_cache.get(run_id)
        if solve_result is None and latest_dec is not None:
            blocks = [
                ScheduledBlockSchema.model_validate(b)
                for b in latest_dec.scheduled_blocks_snapshot
            ]
            solve_result = SolveResult(
                status=latest_dec.status,
                scheduled_blocks=blocks,
                objective_value=latest_dec.objective_value,
                wall_time_seconds=latest_dec.wall_time_seconds,
            )

        return {
            "run_id": run_id,
            "network": network,
            "solve_result": solve_result,
            "model_context": self._context_cache.get(run_id),
            "ripple_report": self._ripple_cache.get(run_id),
            "explanations": self._explain_cache.get(run_id),
        }

    def update_run(self, run_id: str, **kwargs: Any) -> None:
        """Update transient fields of an existing run record.

        Args:
            run_id: Unique run identifier.
            **kwargs: Key-value attributes to store (e.g. solve_result, ripple_report).
        """
        if "solve_result" in kwargs:
            self._solve_cache[run_id] = kwargs["solve_result"]
        if "model_context" in kwargs:
            self._context_cache[run_id] = kwargs["model_context"]
        if "ripple_report" in kwargs:
            self._ripple_cache[run_id] = kwargs["ripple_report"]
        if "explanations" in kwargs:
            self._explain_cache[run_id] = kwargs["explanations"]
        if "network" in kwargs:
            self._network_cache[run_id] = kwargs["network"]

    def has_run(self, run_id: str) -> bool:
        """Check if a run_id exists in the persistent store."""
        with get_session() as session:
            return session.get(NetworkRun, run_id) is not None

    def clear(self) -> None:
        """Clear all stored database runs and in-memory caches (for tests)."""
        create_all_tables()
        with get_session() as session:
            session.query(DecisionRecord).delete()
            session.query(NetworkRun).delete()
        self._network_cache.clear()
        self._solve_cache.clear()
        self._context_cache.clear()
        self._ripple_cache.clear()
        self._explain_cache.clear()


# Module-level singleton instance for API routes
run_store = RunStore()
