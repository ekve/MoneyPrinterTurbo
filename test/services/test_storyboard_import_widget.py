"""Headless tests for Storyboard ZIP uploader state across Streamlit restarts."""

import ast
import io
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.services import storyboard_project


MAIN_PATH = Path(__file__).resolve().parents[2] / "webui" / "Main.py"


class _Rerun(Exception):
    pass


class _StreamlitStub:
    def __init__(self, uploaded=None, import_clicked=False):
        self.session_state = {}
        self.uploaded = uploaded
        self.import_clicked = import_clicked
        self.upload_keys = []
        self.button_disabled = None
        self.messages = []
        self.errors = []

    def markdown(self, text):
        pass

    def info(self, text):
        pass

    def caption(self, text):
        self.messages.append(text)

    def file_uploader(self, label, **kwargs):
        self.upload_keys.append(kwargs["key"])
        return self.uploaded

    def button(self, label, **kwargs):
        assert label == "Import ZIP"
        self.button_disabled = kwargs.get("disabled")
        return self.import_clicked and not self.button_disabled

    def error(self, value):
        self.errors.append(str(value))

    def rerun(self):
        raise _Rerun()


def _import_only_function():
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    original = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_render_storyboard_editor"
    )
    prefix = []
    for stmt in original.body:
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == "setdefault"
            and isinstance(stmt.value.args[0], ast.Constant)
            and stmt.value.args[0].value == "storyboard_project_title_input"
        ):
            break
        prefix.append(stmt)
    else:
        raise AssertionError("could not isolate ZIP import section")
    subset = ast.FunctionDef(
        name="render_import_only",
        args=original.args,
        body=prefix,
        decorator_list=[],
        returns=None,
        type_comment=None,
    )
    return ast.fix_missing_locations(ast.Module(body=[subset], type_ignores=[]))


def _run(st, monkeypatch, import_project=None):
    monkeypatch.setattr(storyboard_project, "import_project", (
        import_project if import_project is not None
        else lambda data: (_ for _ in ()).throw(
            AssertionError("must not attempt import before selecting a file")
        )
    ))
    namespace = {
        "st": st,
        "uuid4": uuid4,
        "storyboard_project": storyboard_project,
        "_infer_tts_server_from_voice": lambda name: "azure-tts-v1",
        "_set_stable_widget_value": lambda *args: None,
    }
    exec(compile(_import_only_function(), str(MAIN_PATH), "exec"), namespace)
    namespace["render_import_only"](SimpleNamespace())


def test_fresh_streamlit_session_has_an_empty_disabled_uploader(monkeypatch):
    st = _StreamlitStub(uploaded=None, import_clicked=True)
    _run(st, monkeypatch)
    assert st.button_disabled is True
    assert not st.errors
    assert any("after restarting" in message for message in st.messages)
    assert st.upload_keys[0].startswith("storyboard_project_import_")


def test_same_session_rerun_keeps_file_widget_key_but_new_session_changes_it(monkeypatch):
    original = _StreamlitStub()
    _run(original, monkeypatch)
    initial_key = original.upload_keys[0]
    _run(original, monkeypatch)
    assert original.upload_keys[-1] == initial_key

    after_restart = _StreamlitStub()
    _run(after_restart, monkeypatch)
    assert after_restart.upload_keys[0] != initial_key


def test_successful_import_rotates_uploader_and_recovers_editor_fields(monkeypatch):
    upload = io.BytesIO(b"valid ZIP bytes")
    upload.name = "taxesai.zip"
    upload.size = len(upload.getvalue())
    st = _StreamlitStub(uploaded=upload, import_clicked=True)
    project = {
        "title": "Taxes AI",
        "voice": "",
        "video_aspect": "16:9",
        "scenes": [{
            "id": "test",
            "type": "generated_image",
            "prompt": "Accountant",
            "duration": 5,
            "duration_mode": "auto",
            "voiceover": "Hello from Taxes AI",
        }],
    }
    imported_bytes = []
    def load(data):
        imported_bytes.append(data)
        return project
    try:
        _run(st, monkeypatch, import_project=load)
    except _Rerun:
        pass
    else:
        raise AssertionError("expected Streamlit rerun after successful import")

    assert imported_bytes == [b"valid ZIP bytes"]
    assert st.button_disabled is False
    assert st.session_state["storyboard_editor_scenes"] == project["scenes"]
    assert st.session_state["storyboard_project_title_input"] == "Taxes AI"
    assert st.session_state["storyboard_pending_script"] == "Hello from Taxes AI"
    assert st.upload_keys[0] != (
        "storyboard_project_import_"
        + st.session_state["storyboard_project_uploader_nonce"]
    )
