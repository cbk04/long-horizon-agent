"""Staged plan-and-execute graph, one module per node role.

- ``planner``   — research pass + structured plan generation (``plan`` node)
- ``executor``  — per-stage ReAct execution with bounded handoff (``react`` node)
- ``evaluator`` — stage verdict against acceptance criteria (``evaluate`` node)
- ``graph``     — approval gate, controller node, graph assembly and entry point
- ``common``    — shared state schema and cross-node helpers
"""

from app.agent.staged.graph import AwaitingApproval, execute_agent, get_graph

__all__ = ["AwaitingApproval", "execute_agent", "get_graph"]
