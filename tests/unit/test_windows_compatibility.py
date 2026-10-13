"""Regression tests for Windows compatibility fixes.

These tests verify that fixes for Windows-specific issues remain in place.
They test the fix exists, not the bug itself (which requires specific Windows environments).

Related issues:
- #75: CLI hangs indefinitely on Windows (asyncio ProactorEventLoop)
- #79: Fix Windows CLI hanging due to asyncio ProactorEventLoop
- #80: Fix Unicode encoding errors on non-English Windows systems
- #89: notebooklm login fails on Windows with Python 3.12 (Playwright subprocess)
- #2485: selector-loop uploads lose data; the CLI now keeps asyncio's default
  (Proactor) loop and only uses the selector loop on request
"""

import asyncio
import os
import sys
import warnings
from unittest.mock import patch

import pytest

from notebooklm._browser.browser_capture import (
    sync_playwright_context as _sync_playwright_context,
)
from notebooklm._browser.browser_capture import (
    windows_playwright_event_loop as _windows_playwright_event_loop,
)


@pytest.mark.requires_playwright
class TestPlaywrightSmokeTest:
    """Smoke tests that actually invoke Playwright to catch real integration issues.

    These tests verify that Playwright can be initialized with our event loop
    configuration. They caught issue #89 (Windows Python 3.12 login failure).
    """

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only smoke test")
    def test_playwright_initializes_with_context_manager(self):
        """Verify sync_playwright() works on Windows with our event loop fix.

        This is a regression test for #89. Without _windows_playwright_event_loop(),
        sync_playwright() raises NotImplementedError on Windows because
        WindowsSelectorEventLoopPolicy doesn't support subprocess spawning.
        """
        with _sync_playwright_context() as p:
            # Just verify Playwright initializes - don't launch a browser
            assert p.chromium is not None
            assert p.firefox is not None
            assert p.webkit is not None

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only smoke test")
    @pytest.mark.usefixtures("restore_event_loop_policy")
    def test_playwright_initializes_after_selector_opt_in(self, monkeypatch):
        """Playwright still starts after the selector opt-in (#89).

        On Python 3.10 the opt-in sets the selector policy only while a command
        runs and then restores it; on 3.11+ the policy is never touched.
        """
        from notebooklm.cli.runtime import run_async

        monkeypatch.setenv("NOTEBOOKLM_WINDOWS_EVENT_LOOP", "selector")
        run_async(asyncio.sleep(0))

        with _sync_playwright_context() as p:
            assert p.chromium is not None

    def test_playwright_initializes_on_non_windows(self):
        """Verify Playwright works normally on non-Windows platforms.

        The context manager should be a no-op, and Playwright should work.
        """
        if sys.platform == "win32":
            pytest.skip("Non-Windows test")

        with _sync_playwright_context() as p:
            assert p.chromium is not None


def _set_selector_policy() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class TestPlaywrightEventLoopFix:
    """Regression tests for Playwright event loop fix (#89).

    Playwright's sync API requires ProactorEventLoop on Windows to spawn browser
    subprocesses. The CLI no longer installs a selector policy, but a Python API
    caller (or the CLI's selector opt-in on Python 3.10) can. The
    _windows_playwright_event_loop() context manager then temporarily restores
    the default policy for Playwright.
    """

    def test_context_manager_exists(self):
        """Verify the context manager exists and is importable."""
        assert callable(_windows_playwright_event_loop)

    def test_context_manager_is_noop_on_non_windows(self):
        """Verify context manager is a no-op on non-Windows platforms."""
        # Mock ``sys.platform`` to non-Windows; the guard reads the same
        # process-wide ``sys`` module object.
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(
                asyncio,
                "set_event_loop_policy",
                side_effect=AssertionError("policy must not change off Windows"),
            ),
            _windows_playwright_event_loop(),
        ):
            pass

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    @pytest.mark.usefixtures("restore_event_loop_policy")
    def test_context_manager_restores_policy_on_windows(self):
        """Verify context manager switches to default policy and restores on exit."""
        _set_selector_policy()

        with _windows_playwright_event_loop():
            inside_policy = asyncio.get_event_loop_policy()
            # Inside the context, should be default (ProactorEventLoop) policy
            assert not isinstance(inside_policy, asyncio.WindowsSelectorEventLoopPolicy), (
                "Context manager should switch to default policy for Playwright"
            )

        # After exit, should restore original policy
        restored_policy = asyncio.get_event_loop_policy()
        assert isinstance(restored_policy, asyncio.WindowsSelectorEventLoopPolicy), (
            "Context manager should restore WindowsSelectorEventLoopPolicy after Playwright"
        )

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    @pytest.mark.usefixtures("restore_event_loop_policy")
    def test_context_manager_restores_on_exception(self):
        """Verify policy is restored even if an exception occurs."""
        _set_selector_policy()

        with pytest.raises(ValueError), _windows_playwright_event_loop():
            raise ValueError("Test exception")

        # Policy should be restored despite exception
        restored_policy = asyncio.get_event_loop_policy()
        assert isinstance(restored_policy, asyncio.WindowsSelectorEventLoopPolicy), (
            "Context manager should restore policy even on exception"
        )


class TestWindowsEventLoopPolicy:
    """The CLI keeps asyncio's default (Proactor) loop on Windows (#2485).

    It used to install WindowsSelectorEventLoopPolicy at import (#79), which
    broke Playwright (#89) and, on CPython 3.12-3.14, lost upload data (#2485).
    The selector loop is now opt-in via NOTEBOOKLM_WINDOWS_EVENT_LOOP. On Python
    3.11+ it is passed to the runner as a loop factory and the global policy is
    untouched; Python 3.10 falls back to setting the policy.
    """

    _CURL_CFFI_ADVICE = (
        "\n    Proactor event loop does not implement add_reader family of methods "
        "required.\n    Registering an additional selector thread for add_reader support."
    )

    def test_windows_runtime_installs_no_event_loop_policy(self, monkeypatch):
        """Simulated Windows startup must leave the event-loop policy alone."""
        from notebooklm import notebooklm_cli

        monkeypatch.setattr(notebooklm_cli.sys, "platform", "win32")
        monkeypatch.setattr(notebooklm_cli.sys, "stdout", None)
        monkeypatch.setattr(notebooklm_cli.sys, "stderr", None)
        monkeypatch.setattr(
            asyncio,
            "set_event_loop_policy",
            lambda policy: pytest.fail("the CLI must not install an event loop policy"),
        )
        monkeypatch.delenv("PYTHONUTF8", raising=False)

        notebooklm_cli._configure_windows_runtime()

    def _emitted_after(self, setup) -> list[str]:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            setup()
            warnings.warn(self._CURL_CFFI_ADVICE, UserWarning, stacklevel=1)
            warnings.warn("an unrelated warning", UserWarning, stacklevel=1)
        return ["curl_cffi" if "Proactor" in str(w.message) else str(w.message) for w in caught]

    def test_cli_main_hides_curl_cffi_proactor_advice_on_windows(self, monkeypatch):
        """The CLI hides curl_cffi's 'use the selector policy' warning; others stay."""
        from notebooklm import notebooklm_cli

        monkeypatch.setattr(notebooklm_cli.sys, "platform", "win32")
        emitted = self._emitted_after(notebooklm_cli._hide_curl_cffi_proactor_advice)
        assert emitted == ["an unrelated warning"]

    def test_curl_cffi_advice_is_kept_off_windows(self, monkeypatch):
        from notebooklm import notebooklm_cli

        monkeypatch.setattr(notebooklm_cli.sys, "platform", "linux")
        emitted = self._emitted_after(notebooklm_cli._hide_curl_cffi_proactor_advice)
        assert emitted == ["curl_cffi", "an unrelated warning"]

    def test_importing_the_cli_does_not_install_the_filter(self, monkeypatch):
        """Applications that import the CLI module keep their warning filters."""
        from notebooklm import notebooklm_cli

        monkeypatch.setattr(notebooklm_cli.sys, "platform", "win32")
        monkeypatch.setattr(notebooklm_cli.sys, "stdout", None)
        monkeypatch.setattr(notebooklm_cli.sys, "stderr", None)
        monkeypatch.delenv("PYTHONUTF8", raising=False)
        emitted = self._emitted_after(notebooklm_cli._configure_windows_runtime)
        assert emitted == ["curl_cffi", "an unrelated warning"]

    @pytest.mark.parametrize(
        ("args", "expect_json"), [(["status"], False), (["status", "--json"], True)]
    )
    def test_bad_event_loop_value_fails_before_the_command_runs(
        self, monkeypatch, tmp_path, args, expect_json
    ):
        """A typo is rejected by the root group, before e.g. a login starts.

        ``status`` needs no event loop and succeeds on its own, so it only
        fails here if the root group checks the variable first.
        """
        import json

        from click.testing import CliRunner

        from notebooklm.cli import runtime
        from notebooklm.notebooklm_cli import cli

        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        monkeypatch.setenv("NOTEBOOKLM_WINDOWS_EVENT_LOOP", "selectr")
        monkeypatch.setattr(runtime.sys, "platform", "win32")
        result = CliRunner().invoke(cli, args)
        monkeypatch.undo()

        assert result.exit_code == 1
        if expect_json:
            payload = json.loads(result.stdout)
            assert payload["code"] == "CONFIG_ERROR"
            assert "NOTEBOOKLM_WINDOWS_EVENT_LOOP='selectr'" in payload["message"]
        else:
            assert "NOTEBOOKLM_WINDOWS_EVENT_LOOP='selectr'" in result.output

    def test_valid_event_loop_value_is_accepted(self, monkeypatch, tmp_path):
        from click.testing import CliRunner

        from notebooklm.cli import runtime
        from notebooklm.notebooklm_cli import cli

        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        monkeypatch.setenv("NOTEBOOKLM_WINDOWS_EVENT_LOOP", "Selector")
        monkeypatch.setattr(runtime.sys, "platform", "win32")
        result = CliRunner().invoke(cli, ["status"])
        monkeypatch.undo()

        assert result.exit_code == 0, result.output

    def test_only_the_owners_touch_the_event_loop_policy(self):
        """Nothing in the package installs a selector policy except the two owners.

        Runs on every OS, so a policy call reintroduced at CLI import fails
        Linux and macOS CI too, not only the Windows cell.
        """
        import ast
        from pathlib import Path

        import notebooklm

        package_root = Path(notebooklm.__file__).parent
        owners = {"cli/runtime.py", "_browser/playwright_event_loop.py"}
        offenders = []
        for path in package_root.rglob("*.py"):
            relative = path.relative_to(package_root).as_posix()
            if relative in owners:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                name = getattr(node, "attr", None) or getattr(node, "id", None)
                if name in {"set_event_loop_policy", "WindowsSelectorEventLoopPolicy"}:
                    offenders.append(f"{relative}:{node.lineno}")
        assert offenders == []

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_cli_import_keeps_default_policy(self):
        """Importing the CLI on Windows must not select the selector loop."""
        from notebooklm.notebooklm_cli import main  # noqa: F401

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            policy = asyncio.get_event_loop_policy()
            selector_policy = asyncio.WindowsSelectorEventLoopPolicy
        assert not isinstance(policy, selector_policy), (
            "The CLI must not force WindowsSelectorEventLoopPolicy; see issue #2485."
        )


class TestWindowsUTF8Mode:
    """Regression tests for Windows UTF-8 encoding fix (#75, #80).

    Non-English Windows systems (cp950, cp932, cp936, etc.) can fail with
    UnicodeEncodeError when outputting Unicode characters like checkmarks.
    The fix sets PYTHONUTF8=1 and reconfigures live stdout/stderr at CLI startup.
    """

    def test_windows_runtime_is_noop_on_non_windows(self, monkeypatch):
        """Non-Windows platforms should not mutate streams or event loop policy."""
        from notebooklm import notebooklm_cli

        class DummyStream:
            def __init__(self):
                self.reconfigure_calls = []

            def reconfigure(self, **kwargs):
                self.reconfigure_calls.append(kwargs)

        stdout = DummyStream()
        stderr = DummyStream()

        monkeypatch.setattr(notebooklm_cli.sys, "platform", "linux")
        monkeypatch.setattr(notebooklm_cli.sys, "stdout", stdout)
        monkeypatch.setattr(notebooklm_cli.sys, "stderr", stderr)
        monkeypatch.setattr(
            asyncio,
            "set_event_loop_policy",
            lambda policy: pytest.fail("event loop policy should not be set on non-Windows"),
        )
        monkeypatch.delenv("PYTHONUTF8", raising=False)

        notebooklm_cli._configure_windows_runtime()

        assert "PYTHONUTF8" not in os.environ
        assert stdout.reconfigure_calls == []
        assert stderr.reconfigure_calls == []

    def test_reconfigure_output_stream_ignores_unsupported_stream(self):
        """Streams without reconfigure support should be ignored."""
        from notebooklm import notebooklm_cli

        notebooklm_cli._reconfigure_output_stream(object())

    def test_reconfigure_output_stream_ignores_missing_stream(self):
        """Detached Windows processes may not have standard streams."""
        from notebooklm import notebooklm_cli

        notebooklm_cli._reconfigure_output_stream(None)

    def test_reconfigure_output_stream_ignores_reconfigure_errors(self):
        """A failed best-effort reconfigure should not abort CLI startup."""
        from notebooklm import notebooklm_cli

        class BrokenStream:
            def reconfigure(self, **kwargs):
                raise OSError("stream is closed")

        notebooklm_cli._reconfigure_output_stream(BrokenStream())

    def test_windows_runtime_reconfigures_active_output_streams(self, monkeypatch):
        """Simulate Windows startup and verify existing streams are made UTF-8-safe."""
        from notebooklm import notebooklm_cli

        class DummyStream:
            encoding = "cp950"

            def __init__(self):
                self.reconfigure_calls = []

            def reconfigure(self, **kwargs):
                self.reconfigure_calls.append(kwargs)

        stdout = DummyStream()
        stderr = DummyStream()

        monkeypatch.setattr(notebooklm_cli.sys, "platform", "win32")
        monkeypatch.setattr(notebooklm_cli.sys, "stdout", stdout)
        monkeypatch.setattr(notebooklm_cli.sys, "stderr", stderr)
        monkeypatch.delenv("PYTHONUTF8", raising=False)

        notebooklm_cli._configure_windows_runtime()

        assert os.environ["PYTHONUTF8"] == "1"
        assert stdout.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]
        assert stderr.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_utf8_mode_enabled(self):
        """Verify UTF-8 mode is enabled on Windows.

        This prevents UnicodeEncodeError on non-English Windows (see issue #75).
        The environment variable should be set in notebooklm_cli.main().
        """
        # Import the CLI main to trigger the UTF-8 setup
        from notebooklm.notebooklm_cli import main  # noqa: F401

        # Check if UTF-8 mode is active (either via flag or env var)
        utf8_enabled = (
            getattr(sys.flags, "utf8_mode", 0) == 1 or os.environ.get("PYTHONUTF8") == "1"
        )
        assert utf8_enabled, (
            "UTF-8 mode must be enabled on Windows to prevent encoding errors. "
            "See issue #75: https://github.com/teng-lin/notebooklm-py/issues/75"
        )


class TestEncodingResilience:
    """Tests for encoding resilience across platforms."""

    @pytest.mark.parametrize(
        "test_char,description",
        [
            ("✓", "checkmark"),
            ("✗", "cross mark"),
            ("📝", "memo emoji"),
            ("→", "arrow"),
            ("•", "bullet"),
        ],
    )
    def test_common_cli_characters_encodable(self, test_char: str, description: str):
        """Verify common CLI output characters can be encoded.

        These characters are used in Rich tables and status output.
        They should either encode successfully or have a fallback.
        """
        # Test that characters can be encoded to UTF-8 (always works)
        try:
            encoded = test_char.encode("utf-8")
            assert len(encoded) > 0
        except UnicodeEncodeError:
            pytest.fail(f"Failed to encode {description} ({test_char!r}) to UTF-8")

    def test_output_with_replace_errors(self):
        """Verify output survives encoding with errors='replace'.

        This simulates the defensive encoding strategy for legacy codepages.
        """
        test_string = "Status: ✓ Complete • 3 items → next"

        # Simulate legacy codepage that can't handle these characters
        try:
            # This would fail on cp950 without errors='replace'
            encoded = test_string.encode("ascii", errors="replace")
            decoded = encoded.decode("ascii")
            # Should have ? replacements but not crash
            assert len(decoded) > 0
            assert "Status" in decoded
        except Exception as e:
            pytest.fail(f"Encoding with errors='replace' should not fail: {e}")
