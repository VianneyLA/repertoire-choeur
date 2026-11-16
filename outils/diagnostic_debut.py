#!/usr/bin/env python3
"""Montre, seconde par seconde, comment commence chaque piste : de quoi distinguer une annonce parlée du chant.

    python3 outils/diagnostic_debut.py _site [secondes]
"""
import json
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from construire import notes_tenues, trames_voix  # noqa: E402

NOTES = ["la", "la#", "si", "do", "do#", "ré", "ré#", "mi", "fa", "fa#", "sol", "sol#"]


def nom_note(dt: float) -> str:
    r = int(round(dt))
    return f"{NOTES[r % 12]}{(r + 9) // 12 + 4}"


def main() -> None:
    site = Path(sys.argv[1])
    limite = float(sys.argv[2]) if len(sys.argv) > 2 else 40
    page = (site / "index.html").read_text(encoding="utf-8")
    donnees = json.loads(re.search(r'<script type="application/json" id="donnees">(.*?)</script>', page, re.S).group(1).replace("<\\/", "</"))
    for c in donnees["chants"]:
        for p in c["pistes"]:
            a = trames_voix(site / p["src"])
            fps = a["fps"]
            tenues = notes_tenues(a, 0.15)
            dans = np.zeros(len(a["db"]), bool)
            for i, j in tenues:
                if (j - i + 1) / fps >= 0.3:
                    dans[i: j + 1] = True
            print(f"\n=== {c['titre']} · {p['nom']} (repère actuel : {p.get('reperes', ['?'])[0]} s) ===")
            print(" s   niveau  voisé  tenu  aigu   notes tenues (≥0,15 s) qui commencent dans la seconde")
            for s in range(int(min(limite, a["duree"]))):
                tr = slice(int(s * fps), int((s + 1) * fps))
                db = a["db"][tr]
                notes = [f"{nom_note(np.median(a['demi_tons'][i:j + 1]))}:{(j - i + 1) / fps:.2f}s"
                         for i, j in tenues if s * fps <= i < (s + 1) * fps]
                print(f"{s:>2}  {np.median(db):>6.0f}  {a['voisee'][tr].mean():>5.0%}  {dans[tr].mean():>4.0%}"
                      f"  {np.mean(a['aigu'][tr][db > -35]) if (db > -35).any() else 0:>4.0%}   " + " ".join(notes))


if __name__ == "__main__":
    main()
