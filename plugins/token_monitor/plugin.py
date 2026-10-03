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
