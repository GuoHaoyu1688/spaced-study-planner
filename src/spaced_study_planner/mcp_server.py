"""Official MCP stdio interface."""

from mcp.server.fastmcp import FastMCP

from .core.planning import stats, today_plan


def make_server(store):
    server = FastMCP("spaced-study-planner")

    @server.tool()
    def get_today_plan(exam: str | None = None) -> dict:
        """Get a capacity-bounded plan with explicit uncertainty and compression suggestions."""
        return today_plan(store, exam)

    @server.tool()
    def next_items(
        n: int = 20, subject: str | None = None, mode: str = "due", tag: str | None = None
    ) -> list[dict]:
        """Read due/new/mistake/topic items; practice material includes answer explanations."""
        return store.next_items(n, subject, mode, tag)

    @server.tool()
    def submit_answer(
        item_id: str,
        answer: str | None = None,
        rating: str | None = None,
        elapsed_s: float = 0,
        request_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        """Record a review. Reuse request_id only for identical retries; stale revisions are rejected."""
        return store.answer(item_id, answer, rating, elapsed_s, request_id, expected_revision)

    @server.tool()
    def get_stats(days: int = 30) -> dict:
        """Read study time, observed accuracy, topic evidence, and completed mock scores."""
        return stats(store, days)

    @server.tool()
    def add_items(items: list[dict], subject: str | None = None) -> dict:
        """Add original or authorized cards/questions atomically; identical IDs skip."""
        return store.add_items(items, subject)

    return server
