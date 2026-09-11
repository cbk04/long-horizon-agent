"""Tests for the dashboard data path (runs / evaluations / budget).

Covers the pure ``_eval_phase_payload`` normalization helper and the
``task_manager`` query functions against an in-memory SQLite session.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.tasks import _eval_phase_payload
from app.harness import task_manager
from app.storage.mysql.database import Base
from app.storage.mysql.models import AgentRun, StageEvaluation, TaskBudgetUsage


def _make_session(tables):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=tables)
    return sessionmaker(bind=engine)()


# ── normalization helper ──────────────────────────────────────────


def test_eval_phase_payload_full_pipeline():
    rule = {"violations": []}
    fwd = {"logical_relevance": "high", "reason": "ok"}
    rev = {"counterexamples": [], "tier": None}
    crits = [{"dimension": "d", "score": 1.0}]
    phase1, phase2, phase3 = _eval_phase_payload(rule, fwd, rev, crits, 0.85)
    assert phase1 == rule
    assert phase2 == {"forward": fwd, "reverse": rev}
    assert phase3 == {"criteria_scores": crits, "weighted_score": 0.85}


def test_eval_phase_payload_rule_short_circuit():
    rule = {"violations": ["阶段产出为空"]}
    phase1, phase2, phase3 = _eval_phase_payload(rule, None, None, None, None)
    assert phase1 == rule
    assert phase2 is None
    assert phase3 is None


def test_eval_phase_payload_phase2_only():
    fwd = {"logical_relevance": "low", "reason": "disconnected"}
    phase1, phase2, phase3 = _eval_phase_payload(None, fwd, None, None, None)
    assert phase1 == {}
    assert phase2 == {"forward": fwd, "reverse": None}
    assert phase3 is None


def test_eval_phase_payload_phase3_only():
    crits = [{"dimension": "d", "score": 0.5}]
    phase1, phase2, phase3 = _eval_phase_payload(None, None, None, crits, 0.5)
    assert phase1 == {}
    assert phase2 is None
    assert phase3 == {"criteria_scores": crits, "weighted_score": 0.5}


# ── task_manager queries ──────────────────────────────────────────


def test_list_runs_filters_by_task_and_orders():
    db = _make_session([AgentRun.__table__])
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    db.add_all([
        AgentRun(id="r1", task_id="t1", thread_id="t1", status="RUNNING",
                 created_at=t0),
        AgentRun(id="r2", task_id="t1", thread_id="t1", status="COMPLETED",
                 created_at=t0 + timedelta(seconds=5)),
        AgentRun(id="r-other", task_id="t2", thread_id="t2", status="RUNNING",
                 created_at=t0),
    ])
    db.commit()

    got = task_manager.list_runs(db, "t1")
    assert [r.id for r in got] == ["r1", "r2"]
    assert all(r.task_id == "t1" for r in got)


def test_list_evaluations_filters_and_orders():
    db = _make_session([StageEvaluation.__table__])
    db.add_all([
        StageEvaluation(id="e1", task_id="t1", run_id="r", stage_index=0,
                        attempt=1, defense_round=0, status="PASSED"),
        StageEvaluation(id="e3", task_id="t1", run_id="r", stage_index=1,
                        attempt=1, defense_round=0, status="DEFEND"),
        StageEvaluation(id="e2", task_id="t1", run_id="r", stage_index=0,
                        attempt=2, defense_round=0, status="RETRY"),
        StageEvaluation(id="e-other", task_id="t2", run_id="r", stage_index=0,
                        attempt=1, defense_round=0, status="PASSED"),
    ])
    db.commit()

    got = task_manager.list_evaluations(db, "t1")
    # order by (stage_index, attempt, defense_round, created_at, id)
    assert [(e.stage_index, e.attempt, e.defense_round) for e in got] == [
        (0, 1, 0),
        (0, 2, 0),
        (1, 1, 0),
    ]


def test_get_budget_usage_returns_row_or_none():
    db = _make_session([TaskBudgetUsage.__table__])
    db.add(TaskBudgetUsage(id=1, task_id="t1", input_tokens=10, output_tokens=20))
    db.commit()

    got = task_manager.get_budget_usage(db, "t1")
    assert got is not None
    assert got.input_tokens == 10
    assert got.output_tokens == 20

    assert task_manager.get_budget_usage(db, "missing") is None
