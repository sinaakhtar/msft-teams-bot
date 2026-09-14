"""BigQuery-as-the-signed-in-user agent for the Teams bot.

Deliberately thin. Importing this package must NOT construct the agent, because
`deploy.py`, the local harness and the runtime entrypoint each want a different
amount of it. Import `bq_agent.agent` for the agent itself.
"""

__all__ = ["agent", "credentials", "errors"]
