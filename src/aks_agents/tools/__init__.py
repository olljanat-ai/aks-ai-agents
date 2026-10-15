"""Tools the agents can be granted. Importing :func:`load_all` registers all of them."""


def load_all() -> None:
    from . import azure, collaboration, core, firewall, kubernetes, repo, web  # noqa: F401
