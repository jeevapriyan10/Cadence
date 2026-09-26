"""Dynamic re-planning benchmark harness for comparing scheduling strategies."""

from datetime import datetime
from typing import Any, Callable, Optional, Union

from cadence.domain.graph import NetworkGraph
from cadence.domain.models import MaintenanceTask, SectionAdjacency, TrackSection, TrainSlot
from cadence.domain.schemas import (
    MaintenanceTaskSchema,
    SectionAdjacencySchema,
    TrackSectionSchema,
    TrainSlotSchema,
)
from cadence.profiles.base import NetworkProfile
from cadence.solve.replan import ReplanResult, replan_full_resolve, replan_warm_start
from cadence.solve.solver import SolveResult

# Handler type for replan strategies to support future extension (e.g. Module 13 'rl')
ReplanStrategyHandler = Callable[..., ReplanResult]

_REPLAN_STRATEGY_REGISTRY: dict[str, ReplanStrategyHandler] = {
    "full_resolve": lambda **kwargs: replan_full_resolve(
        previous_solve_result=kwargs["previous_solve_result"],
        new_emergency_task=kwargs["new_emergency_task"],
        all_existing_tasks=kwargs["all_existing_tasks"],
        sections=kwargs["sections"],
        train_slots=kwargs["train_slots"],
        profile=kwargs["profile"],
        time_horizon_minutes=kwargs["time_horizon_minutes"],
        time_limit_seconds=kwargs["time_limit_seconds"],
        base_time=kwargs.get("base_time"),
        graph=kwargs.get("graph"),
        adjacencies=kwargs.get("adjacencies"),
    ),
    "warm_start": lambda **kwargs: replan_warm_start(
        previous_solve_result=kwargs["previous_solve_result"],
        previous_model_context=kwargs["previous_model_context"],
        new_emergency_task=kwargs["new_emergency_task"],
        all_existing_tasks=kwargs["all_existing_tasks"],
        sections=kwargs["sections"],
        train_slots=kwargs["train_slots"],
        profile=kwargs["profile"],
        time_horizon_minutes=kwargs["time_horizon_minutes"],
        time_limit_seconds=kwargs["time_limit_seconds"],
        base_time=kwargs.get("base_time"),
        graph=kwargs.get("graph"),
        adjacencies=kwargs.get("adjacencies"),
    ),
}


def register_replan_strategy(name: str, handler: ReplanStrategyHandler) -> None:
    """Register an additional re-planning strategy (e.g., 'rl' in Module 13)."""
    _REPLAN_STRATEGY_REGISTRY[name] = handler


def run_replan_benchmark(
    previous_solve_result: SolveResult,
    previous_model_context: dict,
    new_emergency_task: MaintenanceTaskSchema,
    all_existing_tasks: list[Union[MaintenanceTaskSchema, MaintenanceTask]],
    sections: list[Union[TrackSectionSchema, TrackSection]],
    train_slots: list[Union[TrainSlotSchema, TrainSlot]],
    profile: NetworkProfile,
    time_horizon_minutes: int = 1440,
    time_limit_seconds: int = 30,
    base_time: Optional[datetime] = None,
    graph: Optional[NetworkGraph] = None,
    adjacencies: Optional[list[Union[SectionAdjacencySchema, SectionAdjacency]]] = None,
    strategies: Optional[list[str]] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run dynamic re-planning benchmark comparing strategies on an identical scenario.

    Executes replan_full_resolve and replan_warm_start (plus any registered strategies such
    as 'rl' in Module 13), computing speedup factor and signed objective delta.

    Objective Delta Sign Convention:
        objective_delta = warm_start.objective_value - full_resolve.objective_value
        - Negative (< 0): warm_start achieved a lower penalty (better solution quality).
        - Positive (> 0): full_resolve achieved a lower penalty (better solution quality).
        - Zero (0.0): identical objective quality.

    Speedup Factor:
        speedup_factor = full_resolve.wall_time_seconds / warm_start.wall_time_seconds
        - Value > 1.0 indicates warm_start solved faster than full_resolve.

    Args:
        previous_solve_result: The prior SolveResult before emergency task insertion.
        previous_model_context: Model context dictionary from the prior build_cp_model call.
        new_emergency_task: The emergency MaintenanceTask to schedule.
        all_existing_tasks: Pre-existing maintenance tasks in the schedule.
        sections: Track sections in the network.
        train_slots: Scheduled train slots traversing the network.
        profile: NetworkProfile governing priority and disruption constraints.
        time_horizon_minutes: Maximum scheduling horizon in integer minutes (default 1440).
        time_limit_seconds: Maximum wall time allowed for each solver (default 30).
        base_time: Reference datetime for minute 0.
        graph: Optional NetworkGraph representation of topology.
        adjacencies: Optional list of SectionAdjacency connections.
        strategies: Optional list of strategy names to run (defaults to ['full_resolve', 'warm_start']).
        **kwargs: Additional parameters passed to strategy handlers.

    Returns:
        dict[str, Any]: Comparison dictionary structured for clean extension:
            - 'full_resolve': ReplanResult
            - 'warm_start': ReplanResult
            - 'speedup_factor': float
            - 'objective_delta': float | None
            - 'strategies': dict[str, ReplanResult] mapping strategy names to their results
            - 'comparisons': dict[str, dict[str, Any]] comparison metrics against baseline
    """
    call_kwargs = {
        "previous_solve_result": previous_solve_result,
        "previous_model_context": previous_model_context,
        "new_emergency_task": new_emergency_task,
        "all_existing_tasks": all_existing_tasks,
        "sections": sections,
        "train_slots": train_slots,
        "profile": profile,
        "time_horizon_minutes": time_horizon_minutes,
        "time_limit_seconds": time_limit_seconds,
        "base_time": base_time,
        "graph": graph,
        "adjacencies": adjacencies,
        **kwargs,
    }

    target_strategies = strategies or ["full_resolve", "warm_start"]
    results: dict[str, ReplanResult] = {}

    for strat in target_strategies:
        if strat in _REPLAN_STRATEGY_REGISTRY:
            results[strat] = _REPLAN_STRATEGY_REGISTRY[strat](**call_kwargs)
        elif strat == "full_resolve":
            results[strat] = replan_full_resolve(**call_kwargs)
        elif strat == "warm_start":
            results[strat] = replan_warm_start(**call_kwargs)
        else:
            raise ValueError(f"Unknown replan strategy: {strat}")

    # Ensure baseline full_resolve and warm_start references are available
    full_res = results.get("full_resolve")
    warm_res = results.get("warm_start")

    if full_res is None and "full_resolve" in _REPLAN_STRATEGY_REGISTRY:
        full_res = _REPLAN_STRATEGY_REGISTRY["full_resolve"](**call_kwargs)
        results["full_resolve"] = full_res

    if warm_res is None and "warm_start" in _REPLAN_STRATEGY_REGISTRY:
        warm_res = _REPLAN_STRATEGY_REGISTRY["warm_start"](**call_kwargs)
        results["warm_start"] = warm_res

    full_wall_time = full_res.wall_time_seconds if full_res else 0.0
    warm_wall_time = warm_res.wall_time_seconds if warm_res else 0.0
    speedup_factor = float(full_wall_time / max(1e-6, warm_wall_time))

    if (
        warm_res is not None
        and warm_res.objective_value is not None
        and full_res is not None
        and full_res.objective_value is not None
    ):
        objective_delta = float(warm_res.objective_value - full_res.objective_value)
    else:
        objective_delta = None

    # Structured comparison metrics for all strategies vs baseline
    comparisons: dict[str, dict[str, Any]] = {}
    for strat_name, strat_res in results.items():
        if strat_name == "full_resolve":
            continue
        comp_speedup = float(full_wall_time / max(1e-6, strat_res.wall_time_seconds))
        comp_delta = (
            float(strat_res.objective_value - full_res.objective_value)
            if (strat_res.objective_value is not None and full_res is not None and full_res.objective_value is not None)
            else None
        )
        comparisons[strat_name] = {
            "speedup_factor": comp_speedup,
            "objective_delta": comp_delta,
            "wall_time_seconds": strat_res.wall_time_seconds,
            "objective_value": strat_res.objective_value,
            "tasks_changed_count": len(strat_res.tasks_changed),
        }

    return {
        "full_resolve": full_res,
        "warm_start": warm_res,
        "speedup_factor": speedup_factor,
        "objective_delta": objective_delta,
        "strategies": results,
        "comparisons": comparisons,
    }
