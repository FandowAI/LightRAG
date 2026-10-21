"""Small cache identity bridge for the optional prompt admin overlay."""

_digest = ""


def prompt_admin_cache_digest() -> str:
    return _digest


def set_prompt_admin_cache_digest(value: str) -> None:
    global _digest
    _digest = value
