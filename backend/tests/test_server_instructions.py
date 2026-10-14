"""Server instructions for MCP clients: folder names only, never full paths."""

from __future__ import annotations

from types import SimpleNamespace

from rootmind import tools


def _connector(path: str, name: str = "c") -> SimpleNamespace:
    return SimpleNamespace(path=path, name=name)


def test_labels_use_the_last_path_part_only():
    labels = tools.folder_labels(
        [
            _connector("C:/Users/alice/Documents"),
            _connector(r"C:\Users\alice\Desktop\Demo Dokumente" + "\\"),
        ]
    )
    assert labels == ["Documents", "Demo Dokumente"]
    assert "alice" not in tools.server_instructions(labels)


def test_labels_are_deduplicated_capped_and_cleaned():
    connectors = [_connector(f"D:/data/Folder{i % 12}") for i in range(30)]
    connectors.insert(0, _connector("E:/odd\nname\x07here"))
    labels = tools.folder_labels(connectors)
    assert len(labels) == tools.MAX_FOLDER_LABELS
    assert labels[0] == "odd name here"
    assert len(set(labels)) == len(labels)


def test_falls_back_to_the_connector_name():
    assert tools.folder_labels([_connector("", name="docs")]) == ["docs"]


def test_instructions_without_folders():
    text = tools.server_instructions([])
    assert "the local folders indexed by RootMind." in text
