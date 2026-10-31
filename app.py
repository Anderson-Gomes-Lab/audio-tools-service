from flask import Flask, request, jsonify, send_file
import subprocess
import hmac
import json
import logging
import os
import re
import shutil

from werkzeug.exceptions import RequestEntityTooLarge

import fetch
import liens
import montage
import taches

app = Flask(__name__)

# Le dossier de base de nos fichiers : le point de montage de notre volume partagé
BASE_DIR = os.environ.get("MEDIA_DIR", "/media")

# Jeton d'accès : chaque appel envoie l'en-tête "Authorization: Bearer <jeton>".
# Sans API_TOKEN défini (Coolify), tout est refusé, sauf les routes ouvertes.
API_TOKEN = os.environ.get("API_TOKEN")

# Le workflow de transcription n'envoie pas de jeton : ces deux routes restent ouvertes.
# Buffer non plus : lire_lien sert un Short à qui connaît son lien temporaire.
ROUTES_OUVERTES = {"split_audio", "get_duration", "lire_lien"}

# Un video_id entre dans des chemins de fichiers
MOTIF_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}")

taches.reprendre()


@app.before_request
def verifier_jeton():
    if request.endpoint in ROUTES_OUVERTES:
        return None
    recu = request.headers.get("Authorization", "")
    if not API_TOKEN or not hmac.compare_digest(recu.encode(), f"Bearer {API_TOKEN}".encode()):
        return jsonify({"error": "Unauthorized"}), 401


def chemin_dans_media(relative_path):
    """Chemin absolu du fichier, ou None s'il sort de /media (ou n'est pas un texte).

    os.path.join accepte "../" et les chemins absolus : sans cette vérification,
    "../etc/passwd" ou "/etc/passwd" donnerait accès à tout le conteneur.
    """
    if not isinstance(relative_path, str):
        return None
    base = os.path.realpath(BASE_DIR)
    chemin = os.path.realpath(os.path.join(base, relative_path))
    if os.path.commonpath([base, chemin]) != base:
        return None
    return chemin


@app.route('/split', methods=['POST'])
def split_audio():
    data = request.get_json()
    if not data or 'file_path' not in data:
        return jsonify({"error": "Missing file_path"}), 400

    # On construit le chemin complet et sécurisé du fichier
    input_file = chemin_dans_media(data['file_path'])
    if input_file is None:
        return jsonify({"error": "file_path must stay inside /media"}), 400

    if not os.path.exists(input_file):
        return jsonify({"error": f"File not found: {input_file}"}), 404

    output_dir = os.path.dirname(input_file)
    output_pattern = os.path.join(output_dir, "chunk_%03d.mp3")

    command = [
        'ffmpeg',
        '-i', input_file,
        '-f', 'segment',
        '-segment_time', '300',
        '-c', 'copy',
        output_pattern
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return jsonify({"status": "success", "message": f"File split into chunks in {output_dir}"}), 200
    except subprocess.CalledProcessError as e:
        return jsonify({"error": "FFmpeg command failed", "details": e.stderr}), 500

@app.route('/duration', methods=['POST'])
def get_duration():
    data = request.get_json()
    if not data or 'file_path' not in data:
        return jsonify({"error": "Missing file_path"}), 400

    input_file = chemin_dans_media(data['file_path'])
    if input_file is None:
        return jsonify({"error": "file_path must stay inside /media"}), 400

    if not os.path.exists(input_file):
        return jsonify({"error": f"File not found: {input_file}"}), 404

    # La commande ffprobe pour obtenir la durée en secondes
    command = [
        'ffprobe',
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        input_file
    ]

    try:
        # On exécute la commande et on récupère la sortie
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        duration = float(result.stdout.strip())
        return jsonify({"duration_seconds": duration}), 200
    except (subprocess.CalledProcessError, ValueError) as e:
        return jsonify({"error": "Failed to get duration", "details": str(e)}), 500


def video_id_recu():
    """video_id du corps JSON, ou None s'il est absent ou invalide."""
    data = request.get_json(silent=True)
    video_id = data.get('video_id') if isinstance(data, dict) else None
    return video_id if isinstance(video_id, str) and MOTIF_VIDEO.fullmatch(video_id) else None


def dossier_media(nom):
    return os.path.join(os.path.realpath(BASE_DIR), nom)


def chemin_brut(video_id):
    return os.path.join(dossier_media("bruts"), f"{video_id}.mp4")


def chemin_plan(video_id):
    return os.path.join(dossier_media("plans"), f"{video_id}.json")


def lire_video(video_id, remarque=False):
    """/analyse, ou /contexte, qui joue les notes écrites. Un échec après paiement garde son coût (voir lecture.py)."""
    import lecture
    arguments = (video_id, chemin_brut(video_id), dossier_media("lectures"))
    try:
        return lecture.contexte(*arguments) if remarque else lecture.analyser(*arguments)
    except Exception as e:
        noter_cout(getattr(e, "cout_usd", 0))
        raise


def chemin_cout_tache(id_tache):
    return os.path.join(dossier_media("taches"), f"{id_tache}.cout.json")


def noter_cout(cout):
    """Un échec après paiement laisse son coût à côté de l'état de la tâche."""
    if cout:
        try:
            taches.ecrire_json(chemin_cout_tache(taches.en_cours()), {"cout_usd": cout})
        except OSError as ecriture:
            logging.getLogger(__name__).error("coût de la tâche non écrit : %s", ecriture)


def recuperer_video(video_id):
    try:
        return fetch.recuperer(video_id, dossier_media("bruts"))
    except fetch.ErreurFetch as e:
        noter_cout(e.cout_usd)
        raise


def monter_moment(video_id, n):
    if not os.path.isfile(chemin_plan(video_id)):
        raise FileNotFoundError(f"plan absent pour {video_id} : /analyse ne l'écrit plus, le montage revient au lot 2")
    with open(chemin_plan(video_id), encoding="utf-8") as fichier:
        moment = next((m for m in json.load(fichier)["moments"] if m.get("n") == n), None)
    if moment is None:
        raise ValueError(f"moment {n} absent du plan de {video_id}")
    nom = f"{video_id}_{n}.mp4"
    resultat = montage.monter(chemin_brut(video_id), moment, os.path.join(dossier_media("shorts"), nom))
    return {"file_path": f"shorts/{nom}", **resultat}


@app.route('/sante', methods=['GET'])
def sante():
    libre = shutil.disk_usage(os.path.realpath(BASE_DIR)).free
    return jsonify({"disque_libre_go": round(libre / 1e9, 1), "tache_en_cours": taches.en_cours()}), 200


@app.route('/taches/<id_tache>', methods=['GET'])
def etat_tache(id_tache):
    etat = taches.lire(id_tache)
    if etat is None:
        return jsonify({"error": "Unknown task"}), 404
    if etat["etat"] == "reussie":  # n8n ne lit qu'un champ, à la racine, réussie ou échouée
        etat = {**etat, "cout_usd": etat["resultat"].get("cout_usd", 0)}
    elif etat["etat"] == "echouee" and os.path.isfile(chemin_cout_tache(id_tache)):
        with open(chemin_cout_tache(id_tache), encoding="utf-8") as fichier:
            etat = {**etat, **json.load(fichier)}
    return jsonify(etat), 200


@app.route('/fetch', methods=['POST'])
def lancer_fetch():
    video_id = video_id_recu()
    if video_id is None:
        return jsonify({"error": "Invalid video_id"}), 400
    return jsonify({"tache": taches.lancer(lambda: recuperer_video(video_id), video_id)}), 202


@app.route('/analyse', methods=['POST'])
def lancer_analyse():
    video_id = video_id_recu()
    if video_id is None:
        return jsonify({"error": "Invalid video_id"}), 400
    return jsonify({"tache": taches.lancer(lambda: lire_video(video_id), video_id)}), 202


@app.route('/contexte', methods=['POST'])
def lancer_contexte():
    video_id = video_id_recu()
    if video_id is None:
        return jsonify({"error": "Invalid video_id"}), 400
    note = request.get_json().get('note')
    if not isinstance(note, str) or not note.strip():
        return jsonify({"error": "note must be a non-empty string"}), 400
    import lecture
    if lecture.mettre_en_attente(video_id, note.strip(), dossier_media("lectures")):
        return jsonify({"en_attente": True}), 200
    return jsonify({"tache": taches.lancer(lambda: lire_video(video_id, remarque=True), video_id)}), 202


@app.route('/lectures/<video_id>', methods=['GET'])
def lire_lecture(video_id):
    if not MOTIF_VIDEO.fullmatch(video_id):
        return jsonify({"error": "Invalid video_id"}), 400
    import lecture
    lue = lecture.lire(video_id, dossier_media("lectures"))
    return (jsonify(lue), 200) if lue is not None else (jsonify({"error": "Lecture not found"}), 404)


@app.route('/monter', methods=['POST'])
def lancer_montage():
    video_id = video_id_recu()
    if video_id is None:
        return jsonify({"error": "Invalid video_id"}), 400
    n = request.get_json().get('moment')
    if not isinstance(n, int) or isinstance(n, bool):
        return jsonify({"error": "moment must be an integer"}), 400
    return jsonify({"tache": taches.lancer(lambda: monter_moment(video_id, n), video_id)}), 202


@app.route('/shorts/<video_id>/<int:n>', methods=['GET'])
def telecharger_short(video_id, n):
    if not MOTIF_VIDEO.fullmatch(video_id):
        return jsonify({"error": "Invalid video_id"}), 400
    chemin = os.path.join(dossier_media("shorts"), f"{video_id}_{n}.mp4")
    if not os.path.isfile(chemin):
        return jsonify({"error": "Short not found"}), 404
    return send_file(chemin, mimetype="video/mp4", as_attachment=True)


@app.route('/nettoyer', methods=['POST'])
def nettoyer():
    video_id = video_id_recu()
    if video_id is None:
        return jsonify({"error": "Invalid video_id"}), 400
    if taches.video_occupee(video_id):
        return jsonify({"error": "A task is running on this video"}), 409

    fichiers = [chemin_brut(video_id), chemin_brut(video_id) + ".part", chemin_plan(video_id),
                fetch.chemin_suivi(dossier_media("bruts"), video_id)]
    shorts = dossier_media("shorts")
    motif = re.compile(re.escape(video_id) + r"_\d+\.mp4(\.part)?")
    if os.path.isdir(shorts):
        fichiers += [os.path.join(shorts, nom) for nom in os.listdir(shorts) if motif.fullmatch(nom)]
    octets = 0
    for fichier in fichiers:
        if os.path.isfile(fichier):
            octets += os.path.getsize(fichier)
            os.remove(fichier)
    return jsonify({"efface_mo": round(octets / 1e6, 1)}), 200


@app.route('/liens', methods=['POST'])
def creer_lien():
    liens.purger(dossier_media("liens"))
    request.max_content_length = liens.CORPS_MAX
    try:
        return jsonify(liens.creer(request.files.get('fichier'), dossier_media("liens"))), 200
    except RequestEntityTooLarge:
        return jsonify({"error": "File over 300 MB"}), 413
    except liens.Refus as refus:
        return jsonify({"error": str(refus)}), 400


# <path:> : un jeton avec des barres arrive ici et finit en 404, pas en 401 de route inconnue
@app.route('/p/<path:jeton>', methods=['GET'])
def lire_lien(jeton):
    chemin = liens.chemin_valide(jeton, dossier_media("liens"))
    if chemin is None:
        return jsonify({"error": "Not found"}), 404
    try:
        return send_file(chemin, mimetype="video/mp4")
    except FileNotFoundError:  # purgé entre le contrôle et l'envoi
        return jsonify({"error": "Not found"}), 404


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
