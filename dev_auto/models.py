"""Explicit Claude IDs from Anthropic's public model overview, checked 2026-11-07.

This is a version list, not an account entitlement check. Existing aliases remain
automatic; they are never silently rewritten to a pinned model.
"""
CLAUDE_VERSIONS = [
    ("claude-sonnet-5-5", "Sonnet 5.5 · versão fixa"),
    ("claude-opus-5-5", "Opus 5.5 · versão fixa"),
    ("claude-haiku-5-5", "Haiku 5.5 · versão fixa"),
    ("claude-fable-5-1", "Fable 5.1 · versão fixa"),
]
CLAUDE_ALIASES = [
    ("sonnet", "Sonnet · automático (alias)"),
    ("opus", "Opus · automático (alias)"),
    ("haiku", "Haiku · automático (alias)"),
    ("fable", "Fable · automático (alias)"),
]


def model_label(agent, model):
    if model is None:
        return "padrão do CLI"
    if agent == "claude":
        for key, label in [*CLAUDE_VERSIONS, *CLAUDE_ALIASES]:
            if key == model:
                return label
    return model
