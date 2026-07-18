from pathlib import Path

from ns_tool.downloads import build_download_target, save_download_to_path


def test_build_download_target_prefers_url_basename_when_suggested_name_is_gibberish(tmp_path):
    target = build_download_target(
        tmp_path,
        "a1b2c3d4e5f6g7h8i9j0",
        "https://example.com/exports/receipt-2024.pdf?download=1",
    )

    assert target == tmp_path / "receipt-2024.pdf"


def test_build_download_target_keeps_suggested_name_when_it_is_human_readable(tmp_path):
    target = build_download_target(
        tmp_path,
        "Travel declaration.pdf",
        "https://example.com/download",
    )

    assert target == tmp_path / "Travel declaration.pdf"


def test_save_download_to_path_uses_playwright_save_api(tmp_path):
    class FakeDownload:
        def __init__(self) -> None:
            self.saved_to: Path | None = None

        def save_as(self, path: Path) -> None:
            self.saved_to = path
            path.write_bytes(b"ok")

    download = FakeDownload()
    target = tmp_path / "saved.pdf"

    saved = save_download_to_path(download, target)

    assert saved == target
    assert download.saved_to == target
    assert target.read_bytes() == b"ok"
