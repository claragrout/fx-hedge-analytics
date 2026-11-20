"""Fund-level FX risk analytics and hedge recommendations."""

from .analytics import compute_fund_analytics, nav_schedule, xirr, xnpv
from .hedging import generate_hedges
from .ingest import IngestionResult, load_cashflows
from .models import CashflowRecord, CashflowType, DataIssue, HedgeTrade, NavPoint, Severity

__all__ = [
    "CashflowRecord", "CashflowType", "DataIssue", "HedgeTrade", "IngestionResult", "NavPoint",
    "Severity", "compute_fund_analytics", "generate_hedges", "load_cashflows", "nav_schedule",
    "xirr", "xnpv",
]
__version__ = "0.1.0"
