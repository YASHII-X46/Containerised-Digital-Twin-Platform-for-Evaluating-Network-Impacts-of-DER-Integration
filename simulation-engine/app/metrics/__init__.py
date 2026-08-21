"""Pluggable KPI metrics for DER-penetration impact studies."""

from app.metrics.kpi_registry import KpiContext, compute_kpis, kpi_names, register

__all__ = ["KpiContext", "compute_kpis", "kpi_names", "register"]
