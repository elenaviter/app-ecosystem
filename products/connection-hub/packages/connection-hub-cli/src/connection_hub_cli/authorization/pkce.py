"""Compatibility import for KDCube-owned PKCE primitives."""

from kdcube_cli.management.pkce import PKCEParameters, code_challenge, generate_pkce

__all__ = ["PKCEParameters", "code_challenge", "generate_pkce"]
