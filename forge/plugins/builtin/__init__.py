"""Plugins Forge ships with.

Present so a bare name in the manifest resolves without an import path, and so
the first thing an operator reads when writing a plugin is a working one.
Nothing here is loaded unless it is named in `.forge/extensions.json` — shipping
a plugin is not enabling it.
"""
