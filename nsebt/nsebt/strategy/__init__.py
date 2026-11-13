"""Strategies: the frozen interface (base.py), the registry, YAML strategies (spec.py, expr.py)."""
from .base import Context, Output, Strategy, rank_desc, rebalance_days, to_signals

REGISTRY: dict[str, type] = {}


def register(cls):
    """Class decorator: make a Python strategy available by name."""
    if not (isinstance(cls, type) and issubclass(cls, Strategy)):
        raise TypeError("register() takes a Strategy subclass")
    if cls.name in REGISTRY and REGISTRY[cls.name] is not cls:
        raise ValueError(f"strategy '{cls.name}' is already registered")
    REGISTRY[cls.name] = cls
    return cls


def get(name: str, **params) -> Strategy:
    from . import library  # noqa: F401  (registers the Python reference strategies)
    if name not in REGISTRY:
        raise KeyError(f"no Python strategy '{name}'; registered: {sorted(REGISTRY)}")
    return REGISTRY[name](**params)
