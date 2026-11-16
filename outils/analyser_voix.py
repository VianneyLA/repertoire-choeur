#!/usr/bin/env python3
"""Mesure, pour chaque chant du site construit, si ses pistes sont calées entre elles.

    python3 outils/analyser_voix.py _site            les pistes telles que déposées
    python3 outils/analyser_voix.py _site --calees   les pistes recalées pour l'écoute à plusieurs voix

Pour chaque piste : durée, silence au début et à la fin, puis son décalage avec la piste de référence
(la première voix), mesuré sur tout le morceau et fenêtre par fenêtre. Un décalage constant veut dire
que les pistes partent juste à des moments différents ; un décalage qui grandit veut dire un tempo différent.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

SR = 11025
HOP = 110          # environ 100 trames par seconde
FPS = SR / HOP


def charger(chemin: Path) -> np.ndarray:
    brut = subprocess.run(["ffmpeg", "-v", "error", "-i", str(chemin), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                          capture_output=True, check=True).stdout
    return np.frombuffer(brut, dtype="<f4").astype(np.float64)


def enveloppe_attaques(x: np.ndarray) -> np.ndarray:
    """Force des attaques (flux spectral), une valeur par trame."""
    n = 1024
    trames = np.lib.stride_tricks.sliding_window_view(np.pad(x, (0, n)), n)[::HOP]
    spectre = np.log1p(100 * np.abs(np.fft.rfft(trames * np.hanning(n), axis=1)))
    flux = np.maximum(np.diff(spectre, axis=0), 0).sum(axis=1)
    flux = np.concatenate([[0], flux])
    flux -= np.convolve(flux, np.ones(31) / 31, mode="same")  # retire la tendance lente
    return np.maximum(flux, 0)


def enveloppe_energie(x: np.ndarray) -> np.ndarray:
    """Niveau sonore en dB (plancher -40 dB), une valeur par trame : montre les phrases et les respirations."""
    n = len(x) // HOP
    rms = np.sqrt((x[: n * HOP].reshape(n, HOP) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms / rms.max())
    db = np.maximum(db, -40)
    return np.convolve(db, np.ones(10) / 10, mode="same")


def caracteristiques(x: np.ndarray) -> np.ndarray:
    """Attaques et énergie, normalisées puis additionnées : les attaques donnent la précision,
    l'énergie lève l'ambiguïté d'un temps à l'autre."""
    a = enveloppe_attaques(x)
    e = np.diff(enveloppe_energie(x), prepend=0)
    e = np.maximum(e, 0)
    n = min(len(a), len(e))
    norm = lambda v: (v - v.mean()) / (v.std() + 1e-9)
    return norm(a[:n]) + norm(np.convolve(e[:n], np.ones(5) / 5, mode="same"))


def silences(x: np.ndarray) -> tuple[float, float]:
    trame = int(0.05 * SR)
    rms = np.sqrt(np.convolve(x ** 2, np.ones(trame) / trame, mode="same"))
    seuil = rms.max() * 10 ** (-35 / 20)
    actif = np.nonzero(rms > seuil)[0]
    if not len(actif):
        return 0.0, 0.0
    return actif[0] / SR, (len(x) - actif[-1]) / SR


def decalage(ref: np.ndarray, autre: np.ndarray, max_s: float) -> tuple[float, float]:
    """Décalage (s) qui cale `autre` sur `ref` (positif : `autre` est en retard), et la netteté du pic (0 à 1)."""
    a = (ref - ref.mean()) / (ref.std() + 1e-9)
    b = (autre - autre.mean()) / (autre.std() + 1e-9)
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    cc = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    max_l = int(max_s * FPS)
    lags = np.concatenate([np.arange(0, max_l + 1), np.arange(-max_l, 0)])
    vals = np.concatenate([cc[: max_l + 1], cc[-max_l:]]) / min(len(a), len(b))
    i = int(np.argmax(vals))
    return lags[i] / FPS, float(vals[i])


def main() -> None:
    site = Path(sys.argv[1])
    calees = "--calees" in sys.argv
    page = (site / "index.html").read_text(encoding="utf-8")
    donnees = json.loads(re.search(r'<script type="application/json" id="donnees">(.*?)</script>', page, re.S).group(1).replace("<\\/", "</"))
    for c in donnees["chants"]:
        pistes = [dict(p, src=p["mix"]["src"]["1"]) for p in c["pistes"] if p.get("mix")] if calees else c["pistes"]
        if len(pistes) < 2:
            continue
        print(f"\n=== {c['groupe'] + ' – ' if c['groupe'] else ''}{c['titre']} ===")
        sons = {p["nom"]: charger(site / p["src"]) for p in pistes}
        env = {k: caracteristiques(v) for k, v in sons.items()}
        ref_nom = pistes[0]["nom"]
        print(f"{'piste':<12}{'durée':>8}{'silence début':>15}{'silence fin':>13}{'décalage global':>17}{'netteté':>9}")
        for p in pistes:
            nom = p["nom"]
            deb, fin = silences(sons[nom])
            d, nette = decalage(env[ref_nom], env[nom], 30) if nom != ref_nom else (0.0, 1.0)
            print(f"{nom:<12}{len(sons[nom]) / SR:>7.1f}s{deb:>14.2f}s{fin:>12.2f}s{d:>+16.2f}s{nette:>9.2f}")
        print("\nDécalage fenêtre par fenêtre (8 s, toutes les 4 s), par rapport à " + ref_nom + " :")
        r = env[ref_nom]
        larg, pas = int(8 * FPS), int(4 * FPS)
        for p in pistes[1:]:
            nom = p["nom"]
            courant, _ = decalage(r, env[nom], 30)  # suivi : chaque fenêtre part du décalage de la précédente
            ligne = []
            for debut in range(0, len(r) - larg, pas):
                fen_ref = r[debut: debut + larg]
                dep = debut + int(courant * FPS)
                if dep < 0 or dep + larg > len(env[nom]):
                    continue
                d, nette = decalage(fen_ref, env[nom][dep: dep + larg], 1.5)
                if nette > 0.15:
                    courant += d
                ligne.append(f"{debut / FPS:>4.0f}s:{courant:+.2f}({nette:.2f})")
            print(f"  {nom:<10} " + "  ".join(ligne))


if __name__ == "__main__":
    main()
