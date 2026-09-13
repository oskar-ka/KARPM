"""The web UI: status, listings, searches, preferences and config.

Server-rendered HTML. No build step, no npm, no JavaScript framework - the only
script is a fifteen-line poller that refreshes the status panel, so the whole
thing is still readable six months from now.

It runs as its own process (`karpm web`) and talks to the daemon only through
the database: commands go into a queue the daemon polls, so this process needs
no privileges and cannot take the daemon down with it.
"""

from .app import create_app

__all__ = ["create_app"]
