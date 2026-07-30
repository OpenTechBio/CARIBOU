"""Shared, versioned CARIBOU experiment-domain contracts."""

from .ids import new_id
from .lifecycle import (
    EXPERIMENT_TRANSITIONS,
    RUN_TRANSITIONS,
    LifecycleError,
    ExperimentTransitionResult,
    RunTransition,
    create_resume_attempt,
    transition_experiment,
    transition_run,
)
from .models import (
    Aggregate,
    Artifact,
    BudgetRecord,
    Checkpoint,
    Event,
    Experiment,
    ExperimentSpec,
    FailureRecord,
    MetricRecord,
    Run,
)

__all__ = [
    "Aggregate",
    "Artifact",
    "BudgetRecord",
    "Checkpoint",
    "Event",
    "Experiment",
    "ExperimentSpec",
    "FailureRecord",
    "MetricRecord",
    "Run",
    "EXPERIMENT_TRANSITIONS",
    "RUN_TRANSITIONS",
    "LifecycleError",
    "ExperimentTransitionResult",
    "RunTransition",
    "create_resume_attempt",
    "new_id",
    "transition_experiment",
    "transition_run",
]
