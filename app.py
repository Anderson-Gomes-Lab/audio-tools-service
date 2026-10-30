from flask import Flask, request, jsonify
import subprocess
import hmac
import json
import logging
import os
import re
import shutil

import fetch
import montage
import taches
from montage import a_du_son, duree_video, nombre

app = Flask(__name__)

# On définit le dossier de base où se trouvent nos fichiers
# Ce sera le point de montage de notre volume partagé
BASE_DIR = os.environ.get("MEDIA_DIR", "/media")

# Jeton d'accès : chaque appel envoie l'en-tête "Authorization: Bearer <jeton>".
# Sans API_TOKEN défini (Coolify), tout est refusé, sauf les routes ouvertes.
API_TOKEN = os.environ.get("API_TOKEN")

# Le workflow de transcription n'envoie pas de jeton : ces deux routes restent ouvertes
ROUTES_OUVERTES = {"split_audio", "get_duration"}

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


def debut_avant_pic(chemin, longueur, avance=2):
    """Début (s) qui place le moment le plus fort du son à `avance` s du début.

    On mesure le volume de chaque seconde ; le premier pic (à 0,5 dB près) est
    en général l'impact, le rugissement ou le coup de grâce.
    """
    sortie = subprocess.run(
        ['ffmpeg', '-hide_banner', '-nostats', '-i', chemin, '-vn',
         '-af', 'aresample=48000,asetnsamples=n=48000,astats=metadata=1:reset=1,'
                'ametadata=print:key=lavfi.astats.Overall.RMS_level',
         '-f', 'null', '-'],
        capture_output=True, text=True, timeout=300,
    ).stderr
    volumes = [-120.0 if v == '-inf' else float(v)
               for v in re.findall(r'RMS_level=(-?[\d.]+|-inf)', sortie)]
    if len(volumes) <= longueur:
        return 0
    seuil = max(volumes) - 0.5
    pic = next(i for i, v in enumerate(volumes) if v >= seuil)
    return max(0, min(pic - avance, len(volumes) - longueur))


# Les deux mises en page verticales 1080x1920
FILTRES_VERTICAUX = {
    # Image entière au centre, sur un fond fait de la même vidéo agrandie, floutée et assombrie
    "blur": (
        "[0:v]split=2[f][g];"
        "[f]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=25:2,eq=brightness=-0.3[fond];"
        "[g]scale=1080:-2[jeu];"
        "[fond][jeu]overlay=0:(H-h)/2,setsar=1,format=yuv420p[v]"
    ),
    # Découpe d'une bande verticale au centre de l'image, agrandie en plein écran
    "crop": "[0:v]crop=ih*9/16:ih,scale=1080:1920,setsar=1,format=yuv420p[v]",
}


@app.route('/vertical', methods=['POST'])
def vertical():
    """Clip 16:9 -> vidéo verticale 1080x1920 prête pour Shorts, Reels et TikTok.

    JSON attendu :
      file_path    (obligatoire) chemin dans /media
      mode         "blur" (défaut) ou "crop"
      duration     durée de l'extrait en secondes (défaut 15)
      start        début imposé en secondes ; sinon calé 2 s avant le pic sonore
      output_path  chemin de sortie dans /media (défaut : <nom>_vertical_<mode>.mp4)
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'file_path' not in data:
        return jsonify({"error": "Missing file_path"}), 400

    input_file = chemin_dans_media(data['file_path'])
    if input_file is None:
        return jsonify({"error": "file_path must be a path inside /media"}), 400
    if not os.path.isfile(input_file):
        return jsonify({"error": f"File not found: {data['file_path']}"}), 404

    mode = data.get('mode', 'blur')
    if mode not in FILTRES_VERTICAUX:
        return jsonify({"error": "mode must be 'blur' or 'crop'"}), 400

    duree_voulue = nombre(data.get('duration', 15), 1)
    if duree_voulue is None:
        return jsonify({"error": "duration must be a number >= 1"}), 400
    debut_impose = None
    if data.get('start') is not None:
        debut_impose = nombre(data['start'], 0)
        if debut_impose is None:
            return jsonify({"error": "start must be a number >= 0"}), 400

    nom = os.path.splitext(data['file_path'])[0]
    output_file = chemin_dans_media(data.get('output_path') or f"{nom}_vertical_{mode}.mp4")
    if output_file is None:
        return jsonify({"error": "output_path must stay inside /media"}), 400

    try:
        total = duree_video(input_file)
        son = a_du_son(input_file)
        if debut_impose is not None:
            debut = debut_impose
            if debut >= total - 1:
                return jsonify({"error": f"start outside the clip ({total:.0f} s)"}), 400
        else:
            debut = debut_avant_pic(input_file, int(min(duree_voulue, total))) if son else 0
        longueur = min(duree_voulue, total - debut)

        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                   '-ss', str(debut), '-t', str(longueur), '-i', input_file]
        if not son:
            command += montage.ENTREE_SILENCE
        audio = montage.SON_NORMALISE if son else montage.SON_SILENCE
        command += [
            '-filter_complex', FILTRES_VERTICAUX[mode] + ';' + audio,
            '-map', '[v]', '-map', '[a]', '-t', str(longueur),
            *montage.ENCODAGE, output_file,
        ]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
        # Pas de fichier à moitié écrit qui traînerait dans /media
        if os.path.exists(output_file):
            os.remove(output_file)
        details = getattr(e, 'stderr', None) or str(e)
        return jsonify({"error": "Vertical conversion failed",
                        "details": details.replace(os.path.realpath(BASE_DIR), "/media")}), 500

    return jsonify({
        "status": "success",
        "output_path": os.path.relpath(output_file, os.path.realpath(BASE_DIR)),
        "start": debut,
        "duration": longueur,
        "mode": mode,
    }), 200


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


def lire_plan(video_id):
    with open(chemin_plan(video_id), encoding="utf-8") as fichier:
        return json.load(fichier)


def analyser_video(video_id):
    """Plan de la vidéo : celui déjà écrit, sinon appel à Gemini puis tri."""
    if os.path.isfile(chemin_plan(video_id)):
        return lire_plan(video_id)
    brut = chemin_brut(video_id)
    if not os.path.isfile(brut):
        raise FileNotFoundError(f"brut absent pour {video_id} : /fetch d'abord")
    import analyse
    import tri
    plan = analyse.analyser(brut, video_id)
    gardes, rejetes = tri.trier(plan["moments"])
    plan["moments"], plan["rejetes"] = gardes, plan.get("rejetes", []) + rejetes
    taches.ecrire_json(chemin_plan(video_id), plan)
    return plan


def chemin_cout_tache(id_tache):
    return os.path.join(dossier_media("taches"), f"{id_tache}.cout.json")


def recuperer_video(video_id):
    """/fetch : un échec après paiement laisse son coût à côté de l'état de la tâche."""
    try:
        return fetch.recuperer(video_id, dossier_media("bruts"))
    except fetch.ErreurFetch as e:
        if e.cout_usd:
            try:
                taches.ecrire_json(chemin_cout_tache(taches.en_cours()), {"cout_usd": e.cout_usd})
            except OSError as ecriture:
                logging.getLogger(__name__).error("coût de la tâche non écrit : %s", ecriture)
        raise


def monter_moment(video_id, n):
    if not os.path.isfile(chemin_plan(video_id)):
        raise FileNotFoundError(f"plan absent pour {video_id} : /analyse d'abord")
    moment = next((m for m in lire_plan(video_id)["moments"] if m.get("n") == n), None)
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
    if etat["etat"] == "echouee" and os.path.isfile(chemin_cout_tache(id_tache)):
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
    return jsonify({"tache": taches.lancer(lambda: analyser_video(video_id), video_id)}), 202


@app.route('/monter', methods=['POST'])
def lancer_montage():
    video_id = video_id_recu()
    if video_id is None:
        return jsonify({"error": "Invalid video_id"}), 400
    n = request.get_json().get('moment')
    if not isinstance(n, int) or isinstance(n, bool):
        return jsonify({"error": "moment must be an integer"}), 400
    return jsonify({"tache": taches.lancer(lambda: monter_moment(video_id, n), video_id)}), 202


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
