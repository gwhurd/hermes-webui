"""Windows parallel to issue #5175 (macOS launchd git-on-PATH miss).

When the source WebUI server is launched from a Python environment whose PATH
does not include git (e.g. a venv on Windows, mirroring the macOS launchd case),
``shutil.which('git')`` returns None. Before this fix ``_resolve_git_executable``
had only a darwin fallback, so on Windows it returned None -> ``git describe``
never ran -> ``WEBUI_VERSION`` degraded to ``'unknown'`` -> the ``?v=<stamp>``
static-asset cache key froze, so browsers kept serving stale cached JS/CSS even
after the server restarted with fixed code.

The fix adds a Windows fallback that resolves git.exe from the Git-for-Windows
registry ``InstallPath`` (and common install dirs) without relying on PATH.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import api.updates as updates


def _fake_winreg():
    """Minimal winreg stub whose OpenKey/QueryValueEx yield a Git InstallPath."""
    mod = MagicMock()
    mod.HKEY_LOCAL_MACHINE = 0
    mod.HKEY_CURRENT_USER = 1
    mod.KEY_READ = 0x20019
    mod.KEY_WOW64_64KEY = 0x0100
    mod.KEY_WOW64_32KEY = 0x0200

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    mod.OpenKey.return_value = _Key()
    mod.QueryValueEx.return_value = (r'C:\Program Files\Git', 1)
    return mod


def test_resolve_git_executable_uses_registry_on_windows():
    winreg = _fake_winreg()
    expected = os.path.join(r'C:\Program Files\Git', 'cmd', 'git.exe')

    def fake_exists(p):
        return p == expected

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'win32'), \
         patch.dict('sys.modules', {'winreg': winreg}), \
         patch.object(updates.os.path, 'exists', side_effect=fake_exists):
        resolved = updates._resolve_git_executable()

    assert resolved == expected


def test_resolve_git_executable_probes_known_dirs_when_registry_absent():
    winreg = _fake_winreg()
    # Registry has no value -> QueryValueEx raises -> fall through to dir probe.
    winreg.QueryValueEx.side_effect = OSError('no InstallPath')
    program_files = os.environ.get('ProgramFiles', r'C:\Program Files')
    expected = os.path.join(program_files, 'Git', 'cmd', 'git.exe')

    def fake_exists(p):
        return p == expected

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'win32'), \
         patch.dict('sys.modules', {'winreg': winreg}), \
         patch.object(updates.os.path, 'exists', side_effect=fake_exists):
        resolved = updates._resolve_git_executable()

    assert resolved == expected


def test_resolve_git_executable_returns_none_on_windows_when_git_truly_absent():
    winreg = _fake_winreg()
    winreg.QueryValueEx.side_effect = OSError('no InstallPath')

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'win32'), \
         patch.dict('sys.modules', {'winreg': winreg}), \
         patch.object(updates.os.path, 'exists', return_value=False):
        resolved = updates._resolve_git_executable()

    assert resolved is None


def test_non_windows_never_touches_registry_fallback():
    """Falsifiable guard: the registry path must be Windows-only."""
    winreg = _fake_winreg()

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'linux'), \
         patch.dict('sys.modules', {'winreg': winreg}), \
         patch.object(updates.os.path, 'exists', return_value=True):
        resolved = updates._resolve_git_executable()

    assert resolved is None
    winreg.OpenKey.assert_not_called()


def test_detect_webui_version_recovers_via_windows_registry_fallback(tmp_path):
    winreg = _fake_winreg()
    expected_git = os.path.join(r'C:\Program Files\Git', 'cmd', 'git.exe')

    def fake_exists(p):
        # git.exe resolves; no api/_version.py fallback file present.
        return p == expected_git

    def fake_run(cmd, **kwargs):
        assert cmd[0] == expected_git
        if cmd[1:] == ['describe', '--tags', '--always']:
            return MagicMock(returncode=0, stdout='v0.51.999\n', stderr='')
        if cmd[1:] == ['diff-index', '--quiet', 'HEAD', '--']:
            return MagicMock(returncode=0, stdout='', stderr='')
        raise AssertionError(f'unexpected git args: {cmd[1:]!r}')

    with patch.object(updates.shutil, 'which', return_value=None), \
         patch.object(sys, 'platform', 'win32'), \
         patch.dict('sys.modules', {'winreg': winreg}), \
         patch.object(updates.os.path, 'exists', side_effect=fake_exists), \
         patch.object(updates, 'REPO_ROOT', tmp_path), \
         patch.object(updates.subprocess, 'run', side_effect=fake_run):
        version = updates._detect_webui_version()

    assert version == 'v0.51.999'
