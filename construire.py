#!/usr/bin/env python3
"""Construit le site du répertoire à partir du dossier Drive des chefs de chœur.

    python3 construire.py lister        lit le Drive et écrit manifest.json
    python3 construire.py manquants     dit combien de calculs restent à faire (conversions, calages)
    python3 construire.py construire    écrit le site dans _site/

Le Drive se lit avec les variables DRIVE_API_KEY et DRIVE_FOLDER_ID.
Pour essayer sans Drive, sur un dossier de l'ordinateur :
    python3 construire.py lister --local CHEMIN_DU_DOSSIER

Écoute à plusieurs voix : pour chaque chant d'au moins deux voix, chaque piste est recalée sur les autres.
On repère sa première et sa dernière note (ou les repères notés par un chef dans le document « Calage »
du dossier), puis chaque morceau entre deux repères est accéléré ou ralenti, sans changer la hauteur,
pour que les repères de toutes les voix tombent au même moment.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RACINE = Path(__file__).resolve().parent
CACHE = RACINE / ".cache"
SORTIE = RACINE / "_site"
MANIFESTE = RACINE / "manifest.json"
WEB = RACINE / "web"
CONFIG = RACINE / "site.json"

# À augmenter pour refaire tous les calculs (si la façon de convertir ou de caler change).
VERSION_CONVERSION = "2"
VERSION_MIX = "1"
VERSION_NOTES = "2"   # façon de repérer le début et la fin du chant

# Une vidéo (répétition filmée, fichier .mpeg) donne sa piste son.
EXT_AUDIO = {".mp3", ".mpeg", ".mpga", ".mpg", ".m4a", ".aac", ".opus", ".ogg", ".oga", ".wav", ".flac", ".amr",
             ".wma", ".webm", ".mp4", ".m4v", ".mov", ".3gp", ".aif", ".aiff", ".caf"}
EXT_IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
EXT_PDF = {".pdf"}
IGNORES = {".ds_store", "desktop.ini", "thumbs.db"}

NOMS_VOIX = {"sop": "Sopranos", "alt": "Altos", "ten": "Ténors", "bas": "Basses",
             "tut": "Tutti", "acc": "Accompagnement"}
ORDRE_VOIX = ["sop", "alt", "ten", "bas", "tut", "acc", "autre"]
VOIX_MIX = ("sop", "alt", "ten", "bas", "acc")   # ce qui s'écoute ensemble ; un tutti contient déjà tout
MOTIFS_VOIX = [
    ("sop", r"\bsop"),                        # sop, soprano(s), soprani, soprane
    ("alt", r"alt[oi]s?\b|\balt\b"),          # alt, alto(s), alti, contralto
    ("ten", r"\btenor"),                      # ténor(s), tenori
    ("bas", r"\bbass[eiao]?s?\b|\bbaryton"),  # bass, basse(s), bassi, baryton
]
LETTRES = {"s": "sop", "a": "alt", "t": "ten", "b": "bas"}

# Pistes recalées pour l'écoute à plusieurs voix : vitesse, débit mp3.
VITESSES = (("1", 1.0, "48k"), ("0.75", 0.75, "40k"), ("0.5", 0.5, "40k"))
AVANT = 0.15     # secondes gardées avant la première note (l'attaque, la respiration)
APRES = 2.0      # secondes gardées après la dernière note
SILENCE = 1.0    # silence en tête des pistes recalées, avant la première note

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_DOSSIER = "application/vnd.google-apps.folder"
DRIVE_RACCOURCI = "application/vnd.google-apps.shortcut"
DRIVE_DOC = "application/vnd.google-apps.document"
DRIVE_CHAMPS = "id,name,mimeType,modifiedTime,md5Checksum,shortcutDetails"

# « manquants » simule la construction pour compter les calculs ; « utilises » liste les dossiers du cache servis.
TRAVAIL = {"simuler": False, "a_faire": 0, "utilises": set()}


# ---------------------------------------------------------------- noms

def simplifier(texte: str) -> str:
    """Minuscules, sans accents ni ponctuation : « Ténors_2 » → « tenors 2 »."""
    t = unicodedata.normalize("NFKD", texte)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().replace("œ", "oe").replace("æ", "ae")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def slug(texte: str) -> str:
    return re.sub(r"\s+", "-", simplifier(texte)) or "x"


def nom_sans_copie(nom: str) -> str:
    """Retire l'extension et le « (1) » qu'ajoutent les navigateurs aux fichiers en double."""
    return re.sub(r"\s*\(\d+\)\s*$", "", Path(nom).stem)


def nom_affiche(nom: str) -> str:
    """Nom d'une piste sans voix reconnue : « AUD-20261115-WA0001 » devient « Audio du 15/11 »."""
    base = nom_sans_copie(nom)
    m = re.fullmatch(r"(?:AUD|PTT)-\d{4}(\d{2})(\d{2})-WA\d+", base)
    if m:
        return f"Audio du {m.group(2)}/{m.group(1)}"
    return base.replace("_", " ").strip()


def classer_audio(nom: str) -> tuple[str, int | None]:
    """Devine la voix d'un audio d'après son nom : (« sop » | « alt » | … | « autre », numéro)."""
    base = simplifier(nom_sans_copie(nom))
    m = re.fullmatch(r"([satb])\s*([1-4])?", base)
    if m:
        return LETTRES[m.group(1)], int(m.group(2)) if m.group(2) else None
    trouvees = [v for v, motif in MOTIFS_VOIX if re.search(motif, base)]
    if len(trouvees) == 1:
        num = re.search(r"(?:^|\s)([1-4])(?:\s|$)", base)
        return trouvees[0], int(num.group(1)) if num else None
    if not trouvees:
        if re.search(r"\btutti\b|\bensemble\b|\btoutes? (les )?voix\b|\btous\b|\bchoeur complet\b|\bsatb\b", base):
            return "tut", None
        if re.search(r"\bpiano\b|\baccomp|\bplayback\b|\binstru|\bguide\b|\bmetronome\b", base):
            return "acc", None
    return "autre", None


def nom_piste(nom_fichier: str) -> tuple[str, int, str]:
    voix, num = classer_audio(nom_fichier)
    if voix == "autre":
        return voix, 0, nom_affiche(nom_fichier)
    return voix, num or 0, NOMS_VOIX[voix] + (f" {num}" if num else "")


def lire_nom_dossier(nom: str) -> tuple[int | None, str, str]:
    """« 01 – Gluck – Chœur infernal » → (1, « Gluck », « Chœur infernal »)."""
    ordre = None
    m = re.match(r"\s*(\d{1,3})\s*[.)_\-–—]?\s+(?=\S)", nom)
    if m:
        ordre = int(m.group(1))
        nom = nom[m.end():]
    parties = [p.strip() for p in re.split(r"\s+[-–—]\s+|\s*[–—]\s*", nom) if p.strip()]
    if not parties:
        return ordre, "", nom.strip()
    return ordre, " · ".join(parties[:-1]), parties[-1]


def est_calage(nom: str) -> bool:
    return simplifier(nom_sans_copie(nom)).startswith("calage")


def type_fichier(f: dict) -> str | None:
    ext = Path(f["nom"]).suffix.lower()
    mime = f.get("mime") or ""
    if est_calage(f["nom"]) and (mime == DRIVE_DOC or mime.startswith("text/") or ext in (".txt", "")):
        return "calage"
    if ext in EXT_PDF or mime == "application/pdf":
        return "pdf"
    if ext in EXT_AUDIO or mime.startswith(("audio/", "video/")):
        return "audio"
    if ext in EXT_IMAGE or mime.startswith("image/"):
        return "image"
    if mime.startswith("application/vnd.google-apps"):
        return "google"
    return None


# ---------------------------------------------------------------- repères de calage

def lire_temps(texte: str) -> float:
    """« 1:05,2 » → 65.2 ; « 12,4 » → 12.4."""
    texte = texte.replace(",", ".")
    if ":" in texte:
        m, s = texte.split(":", 1)
        return int(m) * 60 + float(s)
    return float(texte)


def ecrire_temps(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}".replace(".", ",")


def lire_calage(texte: str) -> list[tuple[str, list[float]]]:
    """Lignes « Sopranos : 0:12,4 ; 1:05,0 ; 2:38,0 » → [(« Sopranos », [12.4, 65.0, 158.0])]."""
    lignes = []
    for ligne in texte.splitlines():
        m = re.match(r"\s*(.+?)\s*:\s*((?:\d{1,2}:)?\d{1,3}(?:[.,]\d+)?(?:\s*[;|/–-]?\s*(?:\d{1,2}:)?\d{1,3}(?:[.,]\d+)?)*)\s*$", ligne)
        if not m or not re.search(r"[a-zA-Z]", m.group(1)):
            continue
        temps = [lire_temps(t) for t in re.findall(r"(?:\d{1,2}:)?\d{1,3}(?:[.,]\d+)?", m.group(2))]
        lignes.append((m.group(1), temps))
    return lignes


def plan_de_calage(reperes: list[list[float]]) -> list[float]:
    """Repères de chaque voix → positions communes (écart depuis la première note : la médiane des voix)."""
    return [round(statistics.median(r[k] - r[0] for r in reperes), 3) for k in range(len(reperes[0]))]


def souci_de_calage(r: list[float], commun: list[float]) -> str | None:
    """Un morceau à étirer de plus de 25 % trahit presque toujours un repère mal placé."""
    for k in range(1, len(r)):
        rapport = (r[k] - r[k - 1]) / max(commun[k] - commun[k - 1], 0.01)
        if not 0.8 <= rapport <= 1.25:
            sens = "plus lente" if rapport > 1 else "plus rapide"
            return (f"entre {ecrire_temps(r[k - 1])} et {ecrire_temps(r[k])}, cette voix est {abs(rapport - 1):.0%} "
                    f"{sens} que les autres ; un repère est peut-être mal placé")
    return None


# ---------------------------------------------------------------- lecture du Drive

def drive_lire(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        if e.code in (403, 404):
            sys.exit(
                f"Le Drive refuse l'accès ({e.code}). Vérifier que le dossier est partagé "
                f"« Tous les utilisateurs disposant du lien » et que la clé DRIVE_API_KEY autorise "
                f"Google Drive API.\n{detail}"
            )
        sys.exit(f"Le Drive a répondu {e.code}.\n{detail}")


def drive_enfants(dossier: str, cle: str):
    jeton = None
    while True:
        params = {
            "q": f"'{dossier}' in parents and trashed = false",
            "fields": f"nextPageToken,files({DRIVE_CHAMPS})",
            "pageSize": "1000",
            "orderBy": "name",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "key": cle,
        }
        if jeton:
            params["pageToken"] = jeton
        data = json.loads(drive_lire(DRIVE_API + "?" + urllib.parse.urlencode(params)))
        for f in data.get("files", []):
            yield drive_resoudre(f, cle)
        jeton = data.get("nextPageToken")
        if not jeton:
            return


def drive_resoudre(f: dict, cle: str) -> dict:
    """Un raccourci Drive devient le fichier qu'il désigne, sous le nom du raccourci."""
    if f.get("mimeType") != DRIVE_RACCOURCI:
        return f
    cible = (f.get("shortcutDetails") or {}).get("targetId")
    if not cible:
        return f
    params = urllib.parse.urlencode({"fields": DRIVE_CHAMPS, "supportsAllDrives": "true", "key": cle})
    vrai = json.loads(drive_lire(f"{DRIVE_API}/{cible}?{params}"))
    vrai["name"] = f["name"]
    return vrai


def drive_texte(f: dict, cle: str) -> str:
    if f["mime"] == DRIVE_DOC:
        params = urllib.parse.urlencode({"mimeType": "text/plain", "key": cle})
        return drive_lire(f"{DRIVE_API}/{f['id']}/export?{params}").decode("utf-8", "replace")
    params = urllib.parse.urlencode({"alt": "media", "supportsAllDrives": "true", "key": cle})
    return drive_lire(f"{DRIVE_API}/{f['id']}?{params}").decode("utf-8", "replace")


def drive_fichiers(dossier: str, cle: str, prefixe: str = "", profondeur: int = 0):
    for f in drive_enfants(dossier, cle):
        if f["mimeType"] == DRIVE_DOSSIER:
            if profondeur < 3:
                yield from drive_fichiers(f["id"], cle, prefixe + f["name"] + "/", profondeur + 1)
            continue
        entree = {
            "id": f["id"],
            "nom": f["name"],
            "chemin": prefixe + f["name"],
            "mime": f.get("mimeType", ""),
            "modifie": f.get("modifiedTime", ""),
            "empreinte": f.get("md5Checksum") or f.get("modifiedTime", ""),
        }
        if type_fichier(entree) == "calage":
            entree["texte"] = drive_texte(entree, cle)
        yield entree


def lister_drive(racine: str, cle: str) -> dict:
    chants, alertes = [], []
    for f in drive_enfants(racine, cle):
        if f["mimeType"] == DRIVE_DOSSIER:
            chants.append({"id": f["id"], "nom": f["name"], "fichiers": list(drive_fichiers(f["id"], cle))})
        else:
            alertes.append({"chant": "", "fichier": f["name"],
                            "raison": "Posé hors du dossier d'un chant : rangez-le dans le dossier de son chant."})
    return {"chants": chants, "alertes": alertes}


def lister_local(racine: Path) -> dict:
    chants, alertes = [], []
    for d in sorted(racine.iterdir()):
        if d.name.startswith(".") or d.name.lower() in IGNORES:
            continue
        if not d.is_dir():
            alertes.append({"chant": "", "fichier": d.name,
                            "raison": "Posé hors du dossier d'un chant : rangez-le dans le dossier de son chant."})
            continue
        fichiers = []
        for p in sorted(d.rglob("*")):
            if not p.is_file() or any(x.startswith(".") for x in p.relative_to(d).parts):
                continue
            rel = p.relative_to(racine).as_posix()
            entree = {
                "id": hashlib.sha1(rel.encode()).hexdigest()[:16],
                "nom": p.name,
                "chemin": p.relative_to(d).as_posix(),
                "mime": "",
                "modifie": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "empreinte": hashlib.sha1(p.read_bytes()).hexdigest(),
                "local": str(p),
            }
            if type_fichier(entree) == "calage":
                entree["texte"] = p.read_text(encoding="utf-8", errors="replace")
            fichiers.append(entree)
        chants.append({"id": hashlib.sha1(d.name.encode()).hexdigest()[:16], "nom": d.name, "fichiers": fichiers})
    return {"chants": chants, "alertes": alertes}


# ---------------------------------------------------------------- conversion

def cle_cache(f: dict) -> str:
    return hashlib.sha1(f"{VERSION_CONVERSION}:{f['id']}:{f['empreinte']}".encode()).hexdigest()[:16]


def obtenir(f: dict, dest: Path, cle_api: str | None) -> None:
    if f.get("local"):
        shutil.copyfile(f["local"], dest)
        return
    params = urllib.parse.urlencode({"alt": "media", "supportsAllDrives": "true", "key": cle_api or ""})
    try:
        with urllib.request.urlopen(f"{DRIVE_API}/{f['id']}?{params}", timeout=300) as r, open(dest, "wb") as out:
            shutil.copyfileobj(r, out)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"téléchargement refusé par le Drive ({e.code})") from e


def lancer(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} : {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def dimensions(p: Path) -> tuple[int, int]:
    out = lancer(["ffprobe", "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)])
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


def duree_audio(p: Path) -> float:
    return float(lancer(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)]).strip() or 0)


def a_faire() -> None:
    TRAVAIL["a_faire"] += 1


def en_cache(dossier: Path, fabriquer) -> dict | None:
    """Rend le meta.json d'un dossier du cache ; le fabrique s'il manque (ou le compte, en simulation)."""
    meta_path = dossier / "meta.json"
    TRAVAIL["utilises"].add(dossier.name)
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    if TRAVAIL["simuler"]:
        a_faire()
        return None
    tmp = dossier.with_name(dossier.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    meta = fabriquer(tmp)
    (tmp / "meta.json").write_text(json.dumps(meta))
    shutil.rmtree(dossier, ignore_errors=True)
    tmp.rename(dossier)
    return meta


def convertir(f: dict, cle_api: str | None) -> dict | None:
    """Convertit un fichier une seule fois : le résultat reste dans .cache/ d'une construction à l'autre."""
    def fabriquer(tmp: Path) -> dict:
        ext = Path(f["nom"]).suffix.lower()
        source = tmp / ("source" + ext)
        obtenir(f, source, cle_api)
        genre = type_fichier(f)
        if genre == "audio":
            lancer(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-vn", "-ac", "1",
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100",
                    "-codec:a", "libmp3lame", "-b:a", "80k", str(tmp / "piste.mp3")])
            meta = {"type": "audio", "fichier": "piste.mp3", "duree": round(duree_audio(tmp / "piste.mp3"), 2)}
        elif genre == "pdf":
            shutil.copyfile(source, tmp / "partition.pdf")
            lancer(["pdftoppm", "-png", "-scale-to-x", "1300", "-scale-to-y", "-1", str(source), str(tmp / "page")])
            pages = sorted(tmp.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
            if not pages:
                raise RuntimeError("PDF sans page lisible")
            meta = {"type": "pdf", "pdf": "partition.pdf",
                    "pages": [{"fichier": p.name, "l": dimensions(p)[0], "h": dimensions(p)[1]} for p in pages]}
        elif genre == "image":
            sortie = tmp / ("image.png" if ext == ".png" else "image.jpg")
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-frames:v", "1",
                   "-vf", "scale='min(1400,iw)':-2"]
            if sortie.suffix == ".jpg":
                cmd += ["-q:v", "3"]
            lancer(cmd + [str(sortie)])
            l, h = dimensions(sortie)
            meta = {"type": "image", "fichier": sortie.name, "l": l, "h": h}
        else:
            raise RuntimeError("type de fichier non pris en charge")
        source.unlink()
        return meta

    return en_cache(CACHE / cle_cache(f), fabriquer)


def trames_voix(mp3: Path) -> dict:
    """Analyse une piste par trames de 10 ms : niveau (dB sous le maximum), hauteur (demi-tons depuis le la 440),
    voix chantée ou parlée (son périodique), part de souffle aigu (les consonnes « s », « ch », « t »)."""
    import numpy as np

    sr, hop, n = 11025, 110, 512
    brut = subprocess.run(["ffmpeg", "-v", "error", "-i", str(mp3), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                          capture_output=True, check=True).stdout
    x = np.frombuffer(brut, dtype="<f4").astype(np.float64)
    toutes = np.lib.stride_tricks.sliding_window_view(np.pad(x, (0, n)), n)[::hop]
    lmin, lmax = int(sr / 1000), int(sr / 70)
    coupe = int(3000 / sr * 2 * n)   # rang du 3 kHz dans le spectre
    db, lag, nettete, aigu = [], [], [], []
    for b in range(0, len(toutes), 2000):
        trames = toutes[b: b + 2000]
        trames = trames - trames.mean(axis=1, keepdims=True)
        db.append(10 * np.log10((trames ** 2).mean(axis=1) + 1e-12))
        spectre = np.abs(np.fft.rfft(trames * np.hanning(n), 2 * n, axis=1)) ** 2
        aigu.append(spectre[:, coupe:].sum(axis=1) / (spectre.sum(axis=1) + 1e-12))
        auto = np.fft.irfft(spectre, axis=1)[:, :lmax]
        auto /= auto[:, :1] + 1e-12
        l = lmin + np.argmax(auto[:, lmin:lmax], axis=1)
        rangs = np.arange(len(auto))
        net = auto[rangs, l]
        # Période plus courte aussi nette : le son est plus aigu qu'une voix (un clic, un harmonique), on l'écarte.
        sous = np.max([auto[rangs, np.maximum(l // d + e, 1)] for d in (2, 3) for e in (0, 1)], axis=0)
        net = np.where(sous > 0.8 * net, 0.0, net)
        lag.append(l)
        nettete.append(net)
    db, lag, nettete, aigu = (np.concatenate(v) for v in (db, lag, nettete, aigu))
    db -= np.percentile(db, 99)
    demi_tons = 12 * np.log2(sr / lag / 440)
    return {"db": db, "demi_tons": demi_tons, "voisee": (nettete > 0.55) & (db > -28), "aigu": aigu,
            "fps": sr / hop, "duree": len(x) / sr}


def notes_tenues(a: dict, duree_min: float = 0.3) -> list[tuple[int, int]]:
    """Notes tenues : trames voisées consécutives, hauteur stable (vibrato compris), chantées assez fort."""
    import numpy as np

    voisee, demi_tons, db, fps = a["voisee"], a["demi_tons"], a["db"], a["fps"]
    tenues, i = [], 0
    while i < len(voisee):
        if not voisee[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(voisee) and voisee[j + 1] and abs(demi_tons[j + 1] - demi_tons[j]) < 0.8:
            j += 1
        if (j - i + 1) / fps >= duree_min and np.ptp(demi_tons[i: j + 1]) < 1.6 and np.median(db[i: j + 1]) > -20:
            tenues.append((i, j))
        i = j + 1
    return tenues


def detecter_notes(mp3: Path) -> dict:
    """Début et fin du chant dans une piste.

    Une piste commence souvent par une annonce : le chef dit le titre, la voix, parfois chante la note de
    départ. Le chant, lui, se reconnaît à une voix continue pendant plusieurs secondes : le début est la
    première attaque d'un passage où la voix chante presque sans interruption au moins 2 s, et reste chantée
    sur les 6 s qui suivent. Le seuil se règle sur la piste elle-même (une voix grave paraît moins continue).
    La fin est la fin de la dernière note tenue."""
    import numpy as np

    a = trames_voix(mp3)
    db, fps, voisee = a["db"], a["fps"], a["voisee"].astype(float)
    n = len(db)
    if a["duree"] < 3:
        return {"debut": 0.0, "fin": a["duree"], "sur": False}

    def moyenne_devant(x: np.ndarray, largeur: int) -> np.ndarray:
        cumul = np.concatenate([[0.0], np.cumsum(x)])
        fin_ = np.minimum(np.arange(n) + largeur, n)
        return (cumul[fin_] - cumul[:n]) / np.maximum(fin_ - np.arange(n), 1)

    corps = voisee[int(0.2 * n): int(0.8 * n)]
    seuil = max(0.45, 0.85 * float(corps.mean())) if len(corps) else 0.6
    r2 = moyenne_devant(voisee, int(2 * fps))
    r6 = moyenne_devant(voisee, int(6 * fps))
    niveau = moyenne_devant(np.maximum(db, -60), int(2 * fps))
    chante = (r2 >= seuil) & (r6 >= 0.85 * seuil) & (niveau >= -20)
    tenues = notes_tenues(a)
    fin = tenues[-1][1] / fps if tenues else float(np.nonzero(db > -30)[0][-1] / fps)
    if not chante.any():
        return {"debut": round(tenues[0][0] / fps, 2) if tenues else 0.0, "fin": round(fin, 2), "sur": False}
    i = int(np.argmax(chante))
    while i < n - 1 and not (voisee[i] and db[i] > -28):
        i += 1     # première trame chantée du passage
    # Remonte jusqu'à l'attaque : la voix peut avoir commencé un peu avant que le passage soit reconnu.
    limite = max(0, i - int(1.5 * fps))
    trou = 0
    j = i
    while j > limite:
        if voisee[j - 1] and db[j - 1] > -28:
            trou = 0
        else:
            trou += 1
            if trou > int(0.08 * fps):
                break
        j -= 1
    debut = j + trou
    while debut > 0 and db[debut - 1] > db[debut] - 15 and debut > j + trou - int(0.1 * fps):
        debut -= 1
    return {"debut": round(debut / fps, 2), "fin": round(max(fin, debut / fps + 1), 2), "sur": True}


def reperes_auto(f: dict) -> dict | None:
    dossier = CACHE / (cle_cache(f) + "-notes" + VERSION_NOTES)
    return en_cache(dossier, lambda tmp: detecter_notes(CACHE / cle_cache(f) / "piste.mp3"))


def filtre_tempo(rapport: float) -> str:
    """Accélère (rapport > 1) ou ralentit sans changer la hauteur."""
    if abs(rapport - 1) < 0.002:
        return ""
    if FILTRES.get("rubberband"):
        return f",rubberband=tempo={rapport:.5f}"
    filtres, r = [], rapport
    while r < 0.5:
        filtres.append("atempo=0.5")
        r /= 0.5
    filtres.append(f"atempo={r:.5f}")
    return "," + ",".join(filtres)


FILTRES: dict[str, bool] = {}


def verifier_filtres() -> None:
    if not FILTRES:
        try:
            FILTRES["rubberband"] = " rubberband " in lancer(["ffmpeg", "-hide_banner", "-filters"])
        except Exception:
            FILTRES["rubberband"] = False


def rendre_mix(f: dict, reperes: list[float], commun: list[float]) -> dict | None:
    """Piste recalée : SILENCE, puis la musique de la première à la dernière note, chaque morceau entre
    deux repères étiré pour tomber sur les positions communes, puis APRES secondes ; en trois vitesses."""
    cle = hashlib.sha1(json.dumps([VERSION_MIX, cle_cache(f), reperes, commun]).encode()).hexdigest()[:16]

    def fabriquer(tmp: Path) -> dict:
        verifier_filtres()
        source = CACHE / cle_cache(f) / "piste.mp3"
        bornes = [(max(0.0, reperes[0] - AVANT), reperes[0], 1.0)]
        for k in range(1, len(reperes)):
            bornes.append((reperes[k - 1], reperes[k], (reperes[k] - reperes[k - 1]) / (commun[k] - commun[k - 1])))
        bornes.append((reperes[-1], reperes[-1] + APRES, 1.0))
        morceaux = []
        for i, (a, b, rapport) in enumerate(bornes):
            morceaux.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS{filtre_tempo(rapport)}[m{i}]")
        avant = bornes[0][1] - bornes[0][0]
        graphe = ";".join(morceaux) + ";" + "".join(f"[m{i}]" for i in range(len(bornes))) + \
            f"concat=n={len(bornes)}:v=0:a=1,adelay={int((SILENCE - avant) * 1000)}:all=1," \
            f"afade=t=out:st={SILENCE + commun[-1] + APRES - 1:.3f}:d=1[x]"
        cale = tmp / "cale.wav"
        lancer(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-filter_complex", graphe, "-map", "[x]",
                "-ac", "1", "-ar", "24000", str(cale)])
        fichiers = {}
        for nom, vitesse, debit in VITESSES:
            sortie = tmp / f"v{nom}.mp3"
            filtre = filtre_tempo(vitesse)
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(cale)]
            if filtre:
                cmd += ["-af", filtre[1:]]
            lancer(cmd + ["-ac", "1", "-ar", "24000", "-codec:a", "libmp3lame", "-b:a", debit, str(sortie)])
            fichiers[nom] = sortie.name
        cale.unlink()
        carte = [[round(bornes[0][0], 3), round(SILENCE - avant, 3)]]
        carte += [[round(t, 3), round(SILENCE + c, 3)] for t, c in zip(reperes, commun)]
        carte.append([round(reperes[-1] + APRES, 3), round(SILENCE + commun[-1] + APRES, 3)])
        return {"fichiers": fichiers, "carte": carte, "cle": cle}

    return en_cache(CACHE / f"mix-{cle}", fabriquer)


# ---------------------------------------------------------------- construction

def garder_le_plus_recent(fichiers: list[dict]) -> list[dict]:
    """Deux fichiers du même nom (un audio redéposé) : seul le plus récent compte."""
    retenus: dict[tuple, dict] = {}
    for f in fichiers:
        cle = (type_fichier(f), simplifier(nom_sans_copie(f["nom"])))
        if cle not in retenus or f["modifie"] > retenus[cle]["modifie"]:
            retenus[cle] = f
    return sorted(retenus.values(), key=lambda f: simplifier(f["chemin"]))


def copier(source: Path, dest: Path) -> None:
    if not TRAVAIL["simuler"]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)


def caler_chant(c: dict, pistes: list[dict], fichiers_pistes: list[dict], calage: str | None,
                dest: Path, s: str, alertes: list[dict]) -> dict | None:
    """Repère le début et la fin du chant dans chaque piste (ce qui précède le début, une annonce parlée,
    est coupé à l'écoute), puis prépare l'écoute à plusieurs voix : chaque voix recalée sur les autres."""
    notes = {}
    for i, f in enumerate(fichiers_pistes):
        r = reperes_auto(f)
        if r is None:
            return None
        notes[i] = r
    manuels: dict[int, list[float]] = {}
    for nom, temps in lire_calage(calage or ""):
        voix, num, _ = nom_piste(nom)
        cible = [i for i, p in enumerate(pistes) if (p["voix"], p["num"]) == (voix, num)] or \
                [i for i, p in enumerate(pistes) if simplifier(p["nom"]) == simplifier(nom)]
        if not cible:
            alertes.append({"chant": c["nom"], "fichier": "Calage", "raison": f"« {nom} » ne correspond à aucune piste."})
        elif not temps or any(b - a < 0.5 for a, b in zip(temps, temps[1:])):
            alertes.append({"chant": c["nom"], "fichier": "Calage",
                            "raison": f"{nom} : les repères doivent se suivre, à plus d'une demi-seconde d'écart."})
        else:
            manuels[cible[0]] = temps
    for i, p in enumerate(pistes):
        m = manuels.get(i)
        if m and len(m) >= 2:
            p["reperes"] = m
        elif m:   # seul le début est noté
            p["reperes"] = [m[0], max(notes[i]["fin"], m[0] + 1)]
        else:
            p["reperes"] = [notes[i]["debut"], notes[i]["fin"]]
        p["auto"] = i not in manuels
        p["sur"] = bool(m) or notes[i]["sur"]
        # À l'écoute d'une voix seule, on saute ce qui précède le début, avec une marge plus large si le
        # début a été trouvé automatiquement, pour ne pas couper la première note.
        p["coupe"] = round(max(0.0, p["reperes"][0] - (0.3 if m else 0.6)), 2)

    idx = [i for i, p in enumerate(pistes) if p["voix"] in VOIX_MIX]
    if len(idx) < 2:
        return None
    tailles = {len(pistes[i]["reperes"]) for i in idx}
    if len(tailles) == 1:
        reperes = {i: pistes[i]["reperes"] for i in idx}
    else:
        alertes.append({"chant": c["nom"], "fichier": "Calage",
                        "raison": "Les voix n'ont pas le même nombre de repères : seuls le début et la fin comptent."})
        reperes = {i: [pistes[i]["reperes"][0], pistes[i]["reperes"][-1]] for i in idx}
    commun = plan_de_calage([reperes[i] for i in idx])
    for i in idx:
        souci = souci_de_calage(reperes[i], commun)
        if souci:
            alertes.append({"chant": c["nom"], "fichier": pistes[i]["nom"], "raison": f"Calage à vérifier : {souci}."})
    for i in idx:
        meta = rendre_mix(fichiers_pistes[i], reperes[i], commun)
        if meta is None:
            continue
        src = {}
        for nom, fichier in meta["fichiers"].items():
            cible = f"{slug(pistes[i]['nom'])}-cale-{meta['cle'][:8]}-{nom.replace('.', '')}.mp3"
            copier(CACHE / f"mix-{meta['cle']}" / fichier, dest / cible)
            src[nom] = f"m/{s}/{cible}"
        pistes[i]["mix"] = {"src": src, "carte": meta["carte"]}
    return {"duree": round(SILENCE + commun[-1] + APRES, 2), "manuel": any(i in manuels for i in idx),
            "vitesses": [v for v, _, _ in VITESSES]}


def construire(manifeste: dict, cle_api: str | None) -> dict:
    simuler = TRAVAIL["simuler"]
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    alertes = list(manifeste.get("alertes", []))
    chants, slugs = [], set()
    if not simuler:
        shutil.rmtree(SORTIE, ignore_errors=True)
        (SORTIE / "m").mkdir(parents=True)

    for c in manifeste["chants"]:
        ordre, groupe, titre = lire_nom_dossier(c["nom"])
        s = slug(f"{groupe} {titre}")
        if s in slugs:
            s = f"{s}-{c['id'][:6].lower()}"
        slugs.add(s)
        dest = SORTIE / "m" / s
        pistes, fichiers_pistes, pdfs, images, dates = [], [], [], [], []
        calage = None

        for f in c["fichiers"]:
            if f["nom"].lower() in IGNORES or f["nom"].startswith("."):
                continue
            genre = type_fichier(f)
            if genre == "calage":
                calage = f.get("texte", "")
            elif genre == "google":
                alertes.append({"chant": c["nom"], "fichier": f["chemin"],
                                "raison": "Document Google : déposez plutôt un PDF."})
            elif genre is None:
                alertes.append({"chant": c["nom"], "fichier": f["chemin"],
                                "raison": "Type de fichier non lu : déposez un PDF, une photo ou un audio."})

        for f in garder_le_plus_recent([f for f in c["fichiers"] if type_fichier(f) in ("audio", "pdf", "image")]):
            try:
                meta = convertir(f, cle_api)
            except Exception as e:  # un fichier illisible ne bloque pas le reste du site
                alertes.append({"chant": c["nom"], "fichier": f["chemin"], "raison": f"Illisible ({e})."})
                continue
            if meta is None:
                continue
            cle = cle_cache(f)
            dates.append(f["modifie"])
            source = CACHE / cle
            if meta["type"] == "audio":
                voix, num, nom = nom_piste(f["nom"])
                fichier = f"{slug(nom)}-{cle[:8]}.mp3"
                copier(source / meta["fichier"], dest / fichier)
                pistes.append({"voix": voix, "num": num, "nom": nom, "src": f"m/{s}/{fichier}", "duree": meta["duree"]})
                fichiers_pistes.append(f)
            elif meta["type"] == "pdf":
                base = f"{slug(nom_sans_copie(f['nom']))}-{cle[:8]}"
                copier(source / meta["pdf"], dest / f"{base}.pdf")
                pages = []
                for i, p in enumerate(meta["pages"], 1):
                    copier(source / p["fichier"], dest / f"{base}-{i}.png")
                    pages.append({"src": f"m/{s}/{base}-{i}.png", "l": p["l"], "h": p["h"]})
                pdfs.append({"nom": nom_sans_copie(f["nom"]), "pdf": f"m/{s}/{base}.pdf", "pages": pages})
            else:
                fichier = f"{slug(nom_sans_copie(f['nom']))}-{cle[:8]}{Path(meta['fichier']).suffix}"
                copier(source / meta["fichier"], dest / fichier)
                images.append({"src": f"m/{s}/{fichier}", "l": meta["l"], "h": meta["h"]})

        if not pistes and not pdfs and not images:
            continue
        ordre_pistes = sorted(range(len(pistes)), key=lambda i: (ORDRE_VOIX.index(pistes[i]["voix"]), pistes[i]["num"],
                                                                 simplifier(pistes[i]["nom"])))
        pistes = [pistes[i] for i in ordre_pistes]
        fichiers_pistes = [fichiers_pistes[i] for i in ordre_pistes]
        vus: dict[str, int] = {}
        for p in pistes:  # deux pistes au même nom : « Basses », « Basses (2) »
            vus[p["nom"]] = vus.get(p["nom"], 0) + 1
            if vus[p["nom"]] > 1:
                p["nom"] = f"{p['nom']} ({vus[p['nom']]})"
        try:
            mix = caler_chant(c, pistes, fichiers_pistes, calage, dest, s, alertes)
        except Exception as e:
            mix = None
            alertes.append({"chant": c["nom"], "fichier": "Écoute à plusieurs voix", "raison": f"Calage impossible ({e})."})
        for p in pistes:
            del p["num"]
        partitions = pdfs + ([{"nom": "Partition", "pdf": "", "pages": images}] if images else [])
        chant = {"slug": s, "ordre": ordre, "groupe": groupe, "titre": titre,
                 "maj": max(dates), "pistes": pistes, "partitions": partitions}
        if mix and all("mix" in p for p in pistes if p["voix"] in VOIX_MIX):
            chant["mix"] = mix
        chants.append(chant)

    chants.sort(key=lambda c: (c["ordre"] is None, c["ordre"] or 0, not c["groupe"], simplifier(c["groupe"]), simplifier(c["titre"])))
    donnees = {
        "nom": config["nom"],
        "sous_titre": config.get("sous_titre", ""),
        "nouveau_jours": config.get("nouveau_jours", 10),
        "maj": max((c["maj"] for c in chants), default=""),
        "chants": chants,
        "alertes": alertes,
    }
    if simuler:
        return donnees
    json_txt = json.dumps(donnees, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    page = (WEB / "index.html").read_text(encoding="utf-8")
    page = page.replace("__TITRE__", html.escape(config["nom"])).replace("__DONNEES__", json_txt.replace("</", "<\\/"))
    (SORTIE / "index.html").write_text(page, encoding="utf-8")
    shutil.copytree(WEB / "polices", SORTIE / "polices")
    (SORTIE / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    (SORTIE / "version.txt").write_text(hashlib.sha256(page.encode()).hexdigest() + "\n")

    if CACHE.exists():  # le cache ne garde que ce que le site utilise
        for d in CACHE.iterdir():
            if d.name not in TRAVAIL["utilises"]:
                shutil.rmtree(d, ignore_errors=True)
    return donnees


def resumer(donnees: dict) -> str:
    lignes = [f"{len(donnees['chants'])} chant(s) en ligne."]
    for c in donnees["chants"]:
        voix = ", ".join(p["nom"] for p in c["pistes"]) or "aucune piste"
        nb_pages = sum(len(p["pages"]) for p in c["partitions"])
        lignes.append(f"- {(c['groupe'] + ' – ') if c['groupe'] else ''}{c['titre']} : {voix} ; partition : {nb_pages} page(s)")
        if c.get("mix"):
            lignes.append(f"  écoute à plusieurs voix ({'repères des chefs' if c['mix']['manuel'] else 'calage automatique'}) :")
            for p in c["pistes"]:
                if p.get("reperes"):
                    lignes.append(f"    {p['nom']:<12} " + " ; ".join(ecrire_temps(t) for t in p["reperes"]) +
                                  ("" if p.get("sur", True) else "  (notes mal repérées)"))
    if donnees["alertes"]:
        lignes.append(f"\n{len(donnees['alertes'])} remarque(s) :")
        lignes += [f"- {a['chant'] + ' / ' if a['chant'] else ''}{a['fichier']} : {a['raison']}" for a in donnees["alertes"]]
    return "\n".join(lignes)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("etape", choices=["lister", "manquants", "construire"])
    ap.add_argument("--local", type=Path, help="lire un dossier de l'ordinateur au lieu du Drive")
    args = ap.parse_args()
    cle = os.environ.get("DRIVE_API_KEY")

    if args.etape == "lister":
        if args.local:
            manifeste = lister_local(args.local)
        else:
            dossier = os.environ.get("DRIVE_FOLDER_ID", "").strip()
            if not cle or not dossier:
                sys.exit("Il manque DRIVE_API_KEY ou DRIVE_FOLDER_ID (secrets du dépôt GitHub).")
            manifeste = lister_drive(dossier, cle)
        MANIFESTE.write_text(json.dumps(manifeste, ensure_ascii=False, indent=1, sort_keys=True))
        n = sum(len(c["fichiers"]) for c in manifeste["chants"])
        print(f"{len(manifeste['chants'])} dossier(s) de chant, {n} fichier(s).")
        return

    manifeste = json.loads(MANIFESTE.read_text())
    if args.etape == "manquants":
        TRAVAIL["simuler"] = True
        construire(manifeste, cle)
        n = TRAVAIL["a_faire"]
        print(f"{n} calcul(s) à faire.")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as out:
                out.write(f"nombre={n}\n")
        return

    donnees = construire(manifeste, cle)
    texte = resumer(donnees)
    print(texte)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
            out.write(texte + "\n")


if __name__ == "__main__":
    main()
