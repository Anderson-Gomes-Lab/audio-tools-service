import importlib
import subprocess

import pytest


def video(chemin, *options):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=4", *options, str(chemin)],
        check=True,
    )


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("media")
    video(dossier / "clip.mp4", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-shortest")
    video(dossier / "muet.mp4")
    return dossier


@pytest.fixture
def module(media, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(media))
    monkeypatch.setenv("API_TOKEN", "t")
    import app
    return importlib.reload(app)


@pytest.fixture
def client(module):
    return module.app.test_client()


@pytest.fixture
def auth():
    return {"Authorization": "Bearer t"}
