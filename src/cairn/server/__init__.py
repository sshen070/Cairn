"""The Cairn server: a content-addressed store plus an aggregated catalogue.

Runs on the LAN server (a Raspberry Pi 5, in this deployment). Unlike the agent,
the server is allowed third-party dependencies -- it lives on one machine that we
control, rather than on every machine the agent might be installed on.
"""

from .schema import connect  # noqa: F401
