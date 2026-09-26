# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The caller side of Connection Hub: sign-in and the credentials of a Card.

A caller (a person's tool or an agent session) signs in to a Connection Hub
resource, keeps its profiles and credentials in the native store, and opens an
authenticated MCP connection. This is the layer Problem Board's ``pb`` uses; it
needs the ``connection-hub[client]`` extra and no KDCube package (W322).
"""
