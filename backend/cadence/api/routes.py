"""FastAPI HTTP route definitions for Cadence Track/Solve/Ripple/Reason engine."""

from typing import Any
from fastapi import APIRouter, HTTPException, status

from cadence.api.schemas import (
    ErrorResponse,
    ExplainAllResponse,
    ExplainResponse,
    GenerateNetworkResponse,
    GenerateNetworkRequest,
    HistoryResponseSchema,
    ReplanRequestSchema,
    ReplanResponseSchema,
    RippleResponse,
    SolveRequest,
    SolveResponse,
)
from cadence.api.store import run_store
from cadence.domain.db import get_session
from cadence.domain.graph import NetworkGraph
from cadence.domain.schemas import (
    MaintenanceTaskSchema,
    ScheduledBlockSchema,
    SectionAdjacencySchema,
    TrackSectionSchema,
    TrainSlotSchema,
)
from cadence.echo.store import echo_store
from cadence.generator import generate_synthetic_network
from cadence.profiles.registry import ProfileRegistry
from cadence.reason import (
    explain_full_schedule,
    explain_task_scheduling,
    explanation_to_dict,
    format_explanation,
)
from cadence.ripple import format_report_summary, simulate_cascade
from cadence.solve import (
    SolveResult,
    build_cp_model,
    replan_full_resolve,
    replan_warm_start,
    solve_schedule,
)

router = APIRouter()


@router.get(
    "/health",
    summary="Health Check",
    tags=["System"],
)
async def health_check() -> dict[str, str]:
    """Trivial health check verifying that the Cadence API is running and responsive."""
    return {"status": "ok"}


@router.post(
    "/networks/generate",
    response_model=GenerateNetworkResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate Synthetic Network",
    tags=["Track"],
    responses={
        400: {"model": ErrorResponse, "description": "Invalid profile or generation parameters"},
    },
)
async def generate_network(request: GenerateNetworkRequest) -> GenerateNetworkResponse:
    """Generate a reproducible synthetic railway network shaped by a NetworkProfile.

    Builds topology (sections & adjacencies), timetable train slots, and maintenance
    possession tasks, then caches the generated dataset in the active RunStore.
    """
    try:
        net = generate_synthetic_network(
            profile_name=request.profile_name,
            seed=request.seed,
            section_count=request.section_count,
            train_count=request.train_count,
            task_count=request.task_count,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown network profile '{request.profile_name}'. Registered profiles: {ProfileRegistry.list_available()}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Network generation failed: {exc}",
        ) from exc

    with get_session() as session:
        net_run = echo_store.record_network_run(
            session=session,
            profile_name=net["profile_name"],
            seed=net["seed"],
            section_count=len(net["sections"]),
            train_count=len(net["train_slots"]),
            task_count=len(net["maintenance_tasks"]),
        )
        run_id = net_run.id

    run_store.set_network_cache(run_id, net)

    return GenerateNetworkResponse(
        run_id=run_id,
        profile_name=net["profile_name"],
        seed=net["seed"],
        section_count=len(net["sections"]),
        train_count=len(net["train_slots"]),
        task_count=len(net["maintenance_tasks"]),
    )


@router.get(
    "/networks/{run_id}",
    summary="Get Network Details",
    tags=["Track"],
    responses={
        404: {"model": ErrorResponse, "description": "Run ID not found"},
    },
)
async def get_network(run_id: str) -> dict[str, Any]:
    """Retrieve full details of a generated railway network by run_id."""
    run_data = run_store.get_run(run_id)
    if run_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{run_id}' not found.",
        )

    net = run_data.get("network")
    if net is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No network data found for run '{run_id}'.",
        )

    # Serialize domain entities to clean JSON-serializable dictionaries
    sections_json = [
        TrackSectionSchema.model_validate(s).model_dump(mode="json") for s in net["sections"]
    ]
    adjacencies_json = [
        SectionAdjacencySchema.model_validate(a).model_dump(mode="json") for a in net["adjacencies"]
    ]
    train_slots_json = [
        TrainSlotSchema.model_validate(t).model_dump(mode="json") for t in net["train_slots"]
    ]
    tasks_json = [
        MaintenanceTaskSchema.model_validate(m).model_dump(mode="json") for m in net["maintenance_tasks"]
    ]

    return {
        "run_id": run_id,
        "profile_name": net["profile_name"],
        "seed": net["seed"],
        "sections": sections_json,
        "adjacencies": adjacencies_json,
        "train_slots": train_slots_json,
        "maintenance_tasks": tasks_json,
    }


@router.post(
    "/solve",
    response_model=SolveResponse,
    summary="Solve Block Scheduling Problem",
    tags=["Solve"],
    responses={
        400: {"model": ErrorResponse, "description": "No network stored for this run"},
        404: {"model": ErrorResponse, "description": "Run ID not found"},
    },
)
async def solve(request: SolveRequest) -> SolveResponse:
    """Solve the maintenance possession block scheduling problem using Google OR-Tools CP-SAT.

    Enforces structural same-section and safety-adjacency hard constraints, optimizes
    priority weights and train disruption penalties, and stores the resulting schedule.
    """
    run_data = run_store.get_run(request.run_id)
    if run_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{request.run_id}' not found.",
        )

    net = run_data.get("network")
    if net is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Run '{request.run_id}' has no stored network. Generate a network first.",
        )

    profile = ProfileRegistry.get(net["profile_name"])

    solve_result = solve_schedule(
        sections=net["sections"],
        tasks=net["maintenance_tasks"],
        train_slots=net["train_slots"],
        profile=profile,
        time_horizon_minutes=request.time_horizon_minutes,
        time_limit_seconds=request.time_limit_seconds,
        adjacencies=net["adjacencies"],
    )

    # Build model context to support post-hoc explanation in Reason
    model_context = build_cp_model(
        sections=net["sections"],
        tasks=net["maintenance_tasks"],
        train_slots=net["train_slots"],
        profile=profile,
        time_horizon_minutes=request.time_horizon_minutes,
        adjacencies=net["adjacencies"],
    )

    with get_session() as session:
        echo_store.record_decision(
            session=session,
            network_run_id=request.run_id,
            strategy="full_resolve",
            solve_or_replan_result=solve_result,
            is_replan=False,
        )

    run_store.update_run(
        request.run_id,
        solve_result=solve_result,
        model_context=model_context,
    )

    blocks_json = [b.model_dump(mode="json") for b in solve_result.scheduled_blocks]

    return SolveResponse(
        run_id=request.run_id,
        status=solve_result.status,
        scheduled_blocks=blocks_json,
        objective_value=solve_result.objective_value,
        wall_time_seconds=solve_result.wall_time_seconds,
    )


@router.get(
    "/ripple/{run_id}",
    response_model=RippleResponse,
    summary="Simulate Downstream Cascade Delays",
    tags=["Ripple"],
    responses={
        400: {"model": ErrorResponse, "description": "No solve result found for this run"},
        404: {"model": ErrorResponse, "description": "Run ID not found"},
    },
)
async def get_ripple(run_id: str) -> RippleResponse:
    """Simulate downstream cascade delay propagation for a solved schedule using Ripple."""
    run_data = run_store.get_run(run_id)
    if run_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{run_id}' not found.",
        )

    solve_result = run_data.get("solve_result")
    if solve_result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No solve has been executed for run '{run_id}' yet. Run /solve first.",
        )

    net = run_data["network"]
    graph = NetworkGraph.build_from_sections(net["sections"], net["adjacencies"])

    report = simulate_cascade(
        scheduled_blocks=solve_result.scheduled_blocks,
        train_slots=net["train_slots"],
        graph=graph,
        sections=net["sections"],
    )

    run_store.update_run(run_id, ripple_report=report)
    summary_text = format_report_summary(report)

    impacts_json = [imp.model_dump(mode="json") for imp in report.per_train_impacts]

    return RippleResponse(
        run_id=run_id,
        total_trains_affected=report.total_trains_affected,
        total_delay_minutes=report.total_delay_minutes,
        per_train_impacts=impacts_json,
        summary=summary_text,
    )


@router.get(
    "/reason/{run_id}/{task_id}",
    response_model=ExplainResponse,
    summary="Explain Task Scheduling",
    tags=["Reason"],
    responses={
        400: {"model": ErrorResponse, "description": "No solve result found for this run"},
        404: {"model": ErrorResponse, "description": "Run ID or Task ID not found"},
    },
)
async def get_task_explanation(run_id: str, task_id: str) -> ExplainResponse:
    """Reconstruct post-hoc why a specific maintenance task was scheduled in its slot and why alternatives were rejected."""
    run_data = run_store.get_run(run_id)
    if run_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{run_id}' not found.",
        )

    solve_result = run_data.get("solve_result")
    if solve_result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No solve has been executed for run '{run_id}' yet. Run /solve first.",
        )

    net = run_data["network"]
    scheduled_task_ids = {b.task_id for b in solve_result.scheduled_blocks}
    if task_id not in scheduled_task_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' was not scheduled in run '{run_id}'.",
        )

    profile = ProfileRegistry.get(net["profile_name"])
    model_context = run_data.get("model_context") or {}

    explanation = explain_task_scheduling(
        task_id=task_id,
        solve_result=solve_result,
        model_context=model_context,
        sections=net["sections"],
        tasks=net["maintenance_tasks"],
        train_slots=net["train_slots"],
        profile=profile,
    )

    return ExplainResponse(
        run_id=run_id,
        task_id=task_id,
        explanation=explanation_to_dict(explanation),
        summary=format_explanation(explanation),
    )


@router.get(
    "/reason/{run_id}",
    response_model=ExplainAllResponse,
    summary="Explain Full Schedule",
    tags=["Reason"],
    responses={
        400: {"model": ErrorResponse, "description": "No solve result found for this run"},
        404: {"model": ErrorResponse, "description": "Run ID not found"},
    },
)
async def get_full_schedule_explanation(run_id: str) -> ExplainAllResponse:
    """Generate post-hoc explanations for every scheduled maintenance possession task in a run."""
    run_data = run_store.get_run(run_id)
    if run_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{run_id}' not found.",
        )

    solve_result = run_data.get("solve_result")
    if solve_result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No solve has been executed for run '{run_id}' yet. Run /solve first.",
        )

    net = run_data["network"]
    profile = ProfileRegistry.get(net["profile_name"])
    model_context = run_data.get("model_context") or {}

    explanations = explain_full_schedule(
        solve_result=solve_result,
        model_context=model_context,
        sections=net["sections"],
        tasks=net["maintenance_tasks"],
        train_slots=net["train_slots"],
        profile=profile,
    )

    run_store.update_run(run_id, explanations=explanations)

    items = [
        ExplainResponse(
            run_id=run_id,
            task_id=e.task_id,
            explanation=explanation_to_dict(e),
            summary=format_explanation(e),
        )
        for e in explanations
    ]

    return ExplainAllResponse(
        run_id=run_id,
        explanations=items,
    )


@router.post(
    "/replan",
    response_model=ReplanResponseSchema,
    summary="Dynamic Re-planning for Emergency Task",
    tags=["Solve"],
    responses={
        400: {"model": ErrorResponse, "description": "Invalid replan strategy or missing prior solve"},
        404: {"model": ErrorResponse, "description": "Run ID not found"},
    },
)
async def replan(request: ReplanRequestSchema) -> ReplanResponseSchema:
    """Execute dynamic re-planning to integrate an emergency maintenance task.

    Fetches the latest decision via get_latest_decision, invokes the selected strategy
    ('full_resolve', 'warm_start', or 'rl'), persists the new schedule decision record
    with is_replan=True, and returns the replanned outcome.
    """
    run_data = run_store.get_run(request.run_id)
    if run_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{request.run_id}' not found.",
        )

    net = run_data.get("network")
    if net is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Run '{request.run_id}' has no stored network. Generate a network first.",
        )

    with get_session() as session:
        latest_decision = echo_store.get_latest_decision(session, request.run_id)
        if latest_decision is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No prior decision found for run '{request.run_id}'. Run /solve first.",
            )
        prev_status = latest_decision.status
        prev_blocks = [
            ScheduledBlockSchema.model_validate(b)
            for b in latest_decision.scheduled_blocks_snapshot
        ]
        prev_obj = latest_decision.objective_value
        prev_wall_time = latest_decision.wall_time_seconds

    profile = ProfileRegistry.get(net["profile_name"])

    # Reconstruct prior solve result from latest decision
    prev_solve = SolveResult(
        status=prev_status,
        scheduled_blocks=prev_blocks,
        objective_value=prev_obj,
        wall_time_seconds=prev_wall_time,
    )

    strat = request.strategy.lower()
    if strat == "full_resolve":
        replan_res = replan_full_resolve(
            previous_solve_result=prev_solve,
            new_emergency_task=request.new_emergency_task,
            all_existing_tasks=net["maintenance_tasks"],
            sections=net["sections"],
            train_slots=net["train_slots"],
            profile=profile,
            time_horizon_minutes=request.time_horizon_minutes,
            time_limit_seconds=request.time_limit_seconds,
            adjacencies=net["adjacencies"],
        )
    elif strat == "warm_start":
        model_context = run_data.get("model_context")
        if model_context is None:
            model_context = build_cp_model(
                sections=net["sections"],
                tasks=net["maintenance_tasks"],
                train_slots=net["train_slots"],
                profile=profile,
                time_horizon_minutes=request.time_horizon_minutes,
                adjacencies=net["adjacencies"],
            )
        replan_res = replan_warm_start(
            previous_solve_result=prev_solve,
            previous_model_context=model_context,
            new_emergency_task=request.new_emergency_task,
            all_existing_tasks=net["maintenance_tasks"],
            sections=net["sections"],
            train_slots=net["train_slots"],
            profile=profile,
            time_horizon_minutes=request.time_horizon_minutes,
            time_limit_seconds=request.time_limit_seconds,
            adjacencies=net["adjacencies"],
        )
    elif strat == "rl":
        if not request.model_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Strategy 'rl' requires 'model_path' parameter in request body.",
            )
        from cadence.flux.policy_replan import replan_rl
        from cadence.solve.safety import precompute_unsafe_adjacency_pairs

        unsafe_pairs = precompute_unsafe_adjacency_pairs(
            sections=net["sections"],
            adjacencies=net["adjacencies"],
            profile=profile,
        )
        replan_res = replan_rl(
            model_path=request.model_path,
            previous_solve_result=prev_solve,
            new_emergency_task=request.new_emergency_task,
            all_existing_tasks=net["maintenance_tasks"],
            sections=net["sections"],
            train_slots=net["train_slots"],
            profile=profile,
            unsafe_adjacency_pairs=unsafe_pairs,
            time_horizon_minutes=request.time_horizon_minutes,
            time_limit_seconds=request.time_limit_seconds,
            candidate_slots=request.candidate_slots,
            adjacencies=net["adjacencies"],
            previous_model_context=run_data.get("model_context"),
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown replan strategy '{request.strategy}'. Supported: 'full_resolve', 'warm_start', 'rl'",
        )

    # Persist decision via record_decision(is_replan=True)
    with get_session() as session:
        echo_store.record_decision(
            session=session,
            network_run_id=request.run_id,
            strategy=replan_res.strategy,
            solve_or_replan_result=replan_res,
            is_replan=True,
        )

    # Append emergency task to net's maintenance_tasks and update store cache
    em_id = request.new_emergency_task.id
    updated_tasks = [t for t in net["maintenance_tasks"] if (t.id if hasattr(t, "id") else t["id"]) != em_id]
    updated_tasks.append(request.new_emergency_task)
    net["maintenance_tasks"] = updated_tasks

    run_store.update_run(
        request.run_id,
        solve_result=replan_res,
        network=net,
    )

    blocks_json = [b.model_dump(mode="json") for b in replan_res.scheduled_blocks]

    return ReplanResponseSchema(
        strategy=replan_res.strategy,
        status=replan_res.status,
        scheduled_blocks=blocks_json,
        objective_value=replan_res.objective_value,
        wall_time_seconds=replan_res.wall_time_seconds,
        previous_objective_value=replan_res.previous_objective_value,
        tasks_changed=replan_res.tasks_changed,
        rl_fallback_triggered=replan_res.rl_fallback_triggered,
    )


@router.get(
    "/history/{run_id}",
    response_model=list[HistoryResponseSchema],
    summary="Get Run Decision History",
    tags=["History"],
    responses={
        404: {"model": ErrorResponse, "description": "Run ID not found"},
    },
)
async def get_history(run_id: str) -> list[HistoryResponseSchema]:
    """Retrieve full chronological decision history for a railway network run."""
    run_data = run_store.get_run(run_id)
    if run_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{run_id}' not found.",
        )

    with get_session() as session:
        history_records = echo_store.get_run_history(session, run_id)
        history_list = [
            HistoryResponseSchema(
                strategy=rec.strategy,
                status=rec.status,
                objective_value=rec.objective_value,
                wall_time_seconds=rec.wall_time_seconds,
                is_replan=rec.is_replan,
                created_at=rec.created_at,
                rl_fallback_triggered=rec.rl_fallback_triggered,
            )
            for rec in history_records
        ]

    return history_list

