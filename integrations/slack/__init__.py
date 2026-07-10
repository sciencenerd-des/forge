"""Slack bridge for the ``#hermesbot`` channel.

Drives a local Hermes/Forge agent from Slack using Slack Bolt in Socket Mode,
so no public URL (ngrok) is needed — the laptop keeps an outbound WebSocket to
Slack and therefore can run the *local* Hermes control plane.

See :mod:`integrations.slack.bridge` for the app and
:mod:`integrations.slack.client` for the single Hermes integration seam.
"""
