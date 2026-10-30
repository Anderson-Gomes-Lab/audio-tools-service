import json
import subprocess


def post(client, auth, route, corps):
    return client.post(route, json=corps, headers=auth)


def dimensions(chemin):
    sortie = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "json", str(chemin)], check=True, capture_output=True, text=True).stdout
    flux = json.loads(sortie)["streams"][0]
    return flux["width"], flux["height"]


def test_duration_mesure_le_fichier(client, auth):
    reponse = post(client, auth, "/duration", {"file_path": "clip.mp4"})
    assert reponse.status_code == 200
    assert abs(reponse.get_json()["duration_seconds"] - 4) < 0.3


def test_split_decoupe_le_fichier(client, auth, media):
    (media / "audio").mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=2", str(media / "audio" / "a.mp3")], check=True)
    assert post(client, auth, "/split", {"file_path": "audio/a.mp3"}).status_code == 200
    assert (media / "audio" / "chunk_000.mp3").exists()


def test_jeton_exige_sur_les_routes_de_clips(client, media):
    assert client.post("/vertical", json={"file_path": "clip.mp4"}).status_code == 401


def test_sortie_de_media_refusee(client, auth):
    assert post(client, auth, "/duration", {"file_path": "../secret.txt"}).status_code == 400
    assert post(client, auth, "/vertical", {"file_path": "/etc/passwd"}).status_code == 400


def test_vertical_rend_du_1080x1920(client, auth, media):
    reponse = post(client, auth, "/vertical", {"file_path": "clip.mp4", "output_path": "v.mp4", "duration": 2})
    assert reponse.status_code == 200
    assert dimensions(media / "v.mp4") == (1080, 1920)


def test_vertical_clip_muet(client, auth):
    assert post(client, auth, "/vertical", {"file_path": "muet.mp4", "output_path": "m.mp4"}).status_code == 200


def test_vertical_refuse_les_mauvaises_valeurs(client, auth):
    assert post(client, auth, "/vertical", {"file_path": "clip.mp4", "mode": "zoom"}).status_code == 400
    assert post(client, auth, "/vertical", {"file_path": "clip.mp4", "duration": 0}).status_code == 400
    assert post(client, auth, "/vertical", {"file_path": "clip.mp4", "start": 200}).status_code == 400
    assert post(client, auth, "/vertical", {"file_path": 12}).status_code == 400
    assert post(client, auth, "/vertical", {"file_path": "absent.mp4"}).status_code == 404
