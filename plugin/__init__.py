"""Capalis: capability intelligence and Verified Recipes for Hermes Agent."""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    from .capalis.adapters import surfaces
    from .capalis.hermes import ServiceHolder

    holder = ServiceHolder(ctx)
    handlers = surfaces.handlers(holder.get)
    for schema in surfaces.TOOLS:
        ctx.register_tool(name=schema["name"], toolset=surfaces.TOOLSET, schema=schema,
                          handler=handlers[schema["name"]], description=schema["description"])
    ctx.register_command("capalis", surfaces.command(holder.get, ask=getattr(ctx, "ask_choice", None)),
                         description="Capalis status, approvals and controls (/capalis help)",
                         args_hint="[status|setup|approve|show|runs|evidence|pause|resume|revoke|confirm|resolve|help]")
    ctx.register_hook("pre_gateway_dispatch", holder.on_gateway_message)
