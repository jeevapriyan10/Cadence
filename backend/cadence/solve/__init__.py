"""CP-SAT scheduling solver package for Cadence railway block scheduling."""

from cadence.solve.benchmark import register_replan_strategy, run_replan_benchmark
from cadence.solve.model import build_cp_model
from cadence.solve.replan import (
    ReplanResult,
    ReplanStrategy,
    compute_tasks_changed,
    replan_full_resolve,
    replan_warm_start,
)
from cadence.solve.safety import precompute_unsafe_adjacency_pairs
from cadence.solve.solver import SolveResult, solve_schedule

__all__ = [
    "build_cp_model",
    "precompute_unsafe_adjacency_pairs",
    "solve_schedule",
    "SolveResult",
    "ReplanResult",
    "ReplanStrategy",
    "compute_tasks_changed",
    "replan_full_resolve",
    "replan_warm_start",
    "run_replan_benchmark",
    "register_replan_strategy",
]

