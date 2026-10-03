"""Bundled Task Board placeholder."""

from fastapi import Request


def setup(api):
    api.page("/", "Board")

    @api.router.get("/")
    def board(request: Request):
        return api.render(request, "page.html")
