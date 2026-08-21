"""Pluggable power-flow solver backends (each one a standalone container)."""

from app.solvers.client import RemoteSolverEngine, SolverError, SolverTimeout
from app.solvers.registry import (
    available_solvers,
    register_solver,
    resolve_solver_service,
)

__all__ = [
    "RemoteSolverEngine",
    "SolverError",
    "SolverTimeout",
    "available_solvers",
    "register_solver",
    "resolve_solver_service",
]
