from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request

from api.routes import _project_os_workspace_read
from tests._pytest_port import BASE


ROOT = pathlib.Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
WORKSPACE_JS = ROOT / "static" / "workspace.js"


def _get_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.loads(response.read())


def _get_bytes(path: str) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return response.read()


def _post_json(path: str, body: dict | None = None) -> tuple[dict, int]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read()), response.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read()), exc.code


def _make_session(workspace: pathlib.Path) -> str:
    _post_json("/api/workspaces/add", {"path": str(workspace)})
    payload, status = _post_json("/api/session/new", {"workspace": str(workspace)})
    assert status == 200, payload
    return payload["session"]["session_id"]


class TestIssue4582EscapeNavigationLive:
    def test_authorized_dir_list_read_and_raw_stay_virtualized(self, tmp_path):
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (outside / "note.txt").write_text("outside note", encoding="utf-8")
        (workspace / "escape").symlink_to(outside)

        sid = _make_session(workspace)
        root_listing = _get_json(f"/api/list?session_id={sid}&path=.")
        escape_row = {entry["name"]: entry for entry in root_listing["entries"]}["escape"]
        assert escape_row["target_outside_workspace"] is True

        auth, status = _post_json("/api/escape/authorize", {"session_id": sid, "path": "escape"})
        assert status == 200, auth
        assert auth["path"] == "escape"
        assert auth["is_dir"] is True
        assert auth["read_only"] is True

        listed = _get_json(
            f"/api/escape/list?session_id={sid}&token={auth['token']}&path=escape"
        )
        entries = {entry["name"]: entry for entry in listed["entries"]}
        assert listed["path"] == "escape"
        assert listed["read_only"] is True
        assert entries["note.txt"]["path"] == "escape/note.txt"
        assert entries["note.txt"]["escape_read_only"] is True
        assert str(outside) not in json.dumps(listed)

        text = _get_json(
            f"/api/escape/file/read?session_id={sid}&token={auth['token']}&path=escape/note.txt"
        )
        assert text["path"] == "escape/note.txt"
        assert text["content"] == "outside note"
        assert text["escape_read_only"] is True

        raw = _get_bytes(
            f"/api/escape/file/raw?session_id={sid}&token={auth['token']}&path=escape/note.txt"
        )
        assert raw == b"outside note"
        assert _project_os_workspace_read(pathlib.Path(workspace), "escape/note.txt") is None

    def test_nested_escape_row_stays_display_only_and_non_browsable(self, tmp_path):
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        second_outside = tmp_path / "second-outside"
        workspace.mkdir()
        outside.mkdir()
        second_outside.mkdir()
        (second_outside / "secret.txt").write_text("secret", encoding="utf-8")
        (outside / "nested-escape").symlink_to(second_outside)
        (workspace / "escape").symlink_to(outside)

        sid = _make_session(workspace)
        auth, status = _post_json("/api/escape/authorize", {"session_id": sid, "path": "escape"})
        assert status == 200, auth

        listed = _get_json(
            f"/api/escape/list?session_id={sid}&token={auth['token']}&path=escape"
        )
        entries = {entry["name"]: entry for entry in listed["entries"]}
        nested = entries["nested-escape"]
        assert nested["target_outside_workspace"] is True
        assert nested["escape_read_only"] is True
        assert "target" not in nested

        try:
            _get_json(
                f"/api/escape/list?session_id={sid}&token={auth['token']}&path=escape/nested-escape"
            )
            assert False, "nested escape traversal should stay blocked"
        except urllib.error.HTTPError as exc:
            assert exc.code in (403, 404)


class TestIssue4582EscapeNavigationFrontend:
    def test_workspace_route_helper_uses_escape_route_family(self):
        src = WORKSPACE_JS.read_text(encoding="utf-8")
        assert "/api/escape/list?" in src
        assert "/api/escape/file/read?" in src
        assert "/api/escape/file/raw?" in src

    def test_external_rows_authorize_then_open(self):
        src = UI_JS.read_text(encoding="utf-8")
        assert "authorizeWorkspaceEscapeNavigation(item)" in src
        assert "if(grant.isDir) await loadDir(item.path);" in src
        assert "else await openFile(item.path);" in src

    def test_read_only_affordances_stay_suppressed(self):
        ui_src = UI_JS.read_text(encoding="utf-8")
        ws_src = WORKSPACE_JS.read_text(encoding="utf-8")
        assert "if(!isReadOnlyEscape){" in ui_src
        assert "_workspacePathIsReadOnly(_previewCurrentPath)" in ws_src
        assert "_workspacePathIsReadOnly(S.currentDir || '.')" in ws_src
