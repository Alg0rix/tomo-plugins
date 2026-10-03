"""Official usage analytics plugin."""

from fastapi import Request

from . import ledger


def setup(api):
    api.page("/", "Usage")

    @api.router.get("/")
    def usage(request: Request):
        return api.render(request, "page.html")

    @api.router.get("/api/usage")
    def dashboard():
        from app.services import store

        def build(conn):
            data = ledger.dashboard(conn)
            data["agents"] = [
                dict(
                    row,
                    name=(store.get_agent(row["agent_id"]) or {}).get("name")
                    or row["agent_id"],
                )
                for row in data["agents"]
            ]
            return data

        return store.with_db(build)

    def record_turn(ctx):
        from app.services import store

        prompt = int(ctx.prompt_tokens or 0)
        completion = int(ctx.completion_tokens or 0)
        if prompt <= 0 and completion <= 0 and ctx.message.strip():
            from app.runtime.agent.context_usage import estimate_tokens

            prompt = estimate_tokens(ctx.message.strip())
        store.with_db(
            lambda conn: ledger.record_event(
                conn,
                session_id=ctx.session_id,
                agent_id=ctx.agent_id,
                turns=1,
                prompt_tokens=prompt,
                completion_tokens=completion,
                message_preview=ctx.message,
            )
        )

    api.on_turn_end(record_turn)

    def _compact(n: int) -> str:
        for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
            if n >= limit:
                return f"{n / limit:.1f}".rstrip("0").rstrip(".") + suffix
        return str(n)

    def home_card(user_id):
        from app.services import store

        def build(conn):
            return ledger.summary_stats(conn), ledger.heatmap(conn, days=14 * 7)

        summary, days = store.with_db(build)
        today, week = summary["today"], summary["week"]
        if not week["turns"]:
            return {"empty": "No usage recorded yet. Token counts appear after your first chat."}
        card = {
            "metric": {"value": _compact(today["tokens"]), "label": "tokens today"},
            "stats": [
                {"label": "this week", "value": _compact(week["tokens"])},
                {"label": "turns", "value": str(week["turns"])},
                {"label": "chats", "value": str(week["sessions"])},
            ],
            "heatmap": {"values": [d["tokens"] for d in days], "label": "Last 14 weeks"},
            "actions": [{"label": "Usage", "href": api.base_url + "/"}],
        }
        if summary["active_sessions_1h"]:
            card["status"] = {"text": f"{summary['active_sessions_1h']} active", "tone": "ok"}
        return card

    if hasattr(api, "home_card"):
        try:
            api.home_card(home_card, title="Token usage", kanji="算")
        except (TypeError, ValueError):  # Tomo before rich Home cards
            api.home_card(home_card, title="Token usage")
