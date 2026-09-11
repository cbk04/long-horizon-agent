"""Agent middleware package.

``MessagePersistenceMiddleware`` mirrors the react thread's messages into the
``agent_message`` table (business-side, queryable) after each model call.
"""

from app.agent.middleware.message_persistence import MessagePersistenceMiddleware

__all__ = ["MessagePersistenceMiddleware"]
