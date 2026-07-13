"""Executable isolation tests for Vault's private MCP request metadata."""
from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import server


_METADATA_KEY = "com.southwestcremation.vault/convex-user-token"


def _contextvar_binder():
    """A task-local stand-in for the generic Hermes-core binder."""
    current = contextvars.ContextVar("vault_mcp_metadata", default=None)
    calls = []

    @contextmanager
    def bind_mcp_request_metadata(server_name, metadata):
        calls.append((server_name, metadata))
        token = current.set(metadata)
        try:
            yield
        finally:
            current.reset(token)

    return current, calls, bind_mcp_request_metadata


def test_vault_scope_binds_only_vault_mcp_metadata_and_resets_after_success():
    current, calls, binder = _contextvar_binder()

    with patch("tools.mcp_request_metadata.mcp_request_metadata", binder, create=True):
        with server._vault_mcp_metadata_scope("vault-token-a"):
            assert current.get() == {_METADATA_KEY: "vault-token-a"}

    assert current.get() is None
    assert calls == [("vault-mcp", {_METADATA_KEY: "vault-token-a"})]


def test_vault_scope_resets_after_exception_without_contaminating_next_turn():
    current, _calls, binder = _contextvar_binder()

    with patch("tools.mcp_request_metadata.mcp_request_metadata", binder, create=True):
        try:
            with server._vault_mcp_metadata_scope("vault-token-a"):
                assert current.get() == {_METADATA_KEY: "vault-token-a"}
                raise RuntimeError("token-specific failure")
        except RuntimeError:
            pass

        assert current.get() is None
        with server._vault_mcp_metadata_scope("vault-token-b"):
            assert current.get() == {_METADATA_KEY: "vault-token-b"}

    assert current.get() is None


def test_parallel_vault_scopes_observe_distinct_task_local_metadata():
    current, _calls, binder = _contextvar_binder()
    barrier = threading.Barrier(2)

    def run(token):
        with server._vault_mcp_metadata_scope(token):
            barrier.wait(timeout=2)
            return current.get()

    with patch("tools.mcp_request_metadata.mcp_request_metadata", binder, create=True):
        with ThreadPoolExecutor(max_workers=2) as pool:
            observed = list(pool.map(run, ("vault-token-a", "vault-token-b")))

    assert observed == [
        {_METADATA_KEY: "vault-token-a"},
        {_METADATA_KEY: "vault-token-b"},
    ]
    assert current.get() is None


def test_non_vault_scope_does_not_import_or_bind_private_metadata():
    with server._vault_mcp_metadata_scope(None):
        pass
