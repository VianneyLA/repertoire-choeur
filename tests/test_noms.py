import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from construire import (classer_audio, ecrire_temps, garder_le_plus_recent, lire_calage, lire_nom_dossier,  # noqa: E402
                        nom_affiche, plan_de_calage, souci_de_calage, type_fichier)


class Voix(unittest.TestCase):
    def test_noms_simples(self):
        for nom, voix in [("Sopranos.mp3", "sop"), ("soprano.m4a", "sop"), ("Altos.opus", "alt"), ("alti.mp3", "alt"),
                          ("Ténors.mp3", "ten"), ("TENOR.wav", "ten"), ("Basses.mp3", "bas"), ("Basse.ogg", "bas"),
                          ("Contralto.mp3", "alt"), ("Barytons.mp3", "bas")]:
            self.assertEqual(classer_audio(nom), (voix, None), nom)

    def test_noms_whatsapp(self):
        self.assertEqual(classer_audio("Choeur infernal altos.opus"), ("alt", None))
        self.assertEqual(classer_audio("Orphée gluck sopranos.opus"), ("sop", None))
        self.assertEqual(classer_audio("AUD-20261115-WA0001.opus"), ("autre", None))

    def test_numeros_et_lettres(self):
        self.assertEqual(classer_audio("Basses 2.mp3"), ("bas", 2))
        self.assertEqual(classer_audio("Sopranos_1.mp3"), ("sop", 1))
        self.assertEqual(classer_audio("S1.mp3"), ("sop", 1))
        self.assertEqual(classer_audio("T.mp3"), ("ten", None))

    def test_tutti_et_piano(self):
        self.assertEqual(classer_audio("Tutti.mp3"), ("tut", None))
        self.assertEqual(classer_audio("Toutes les voix.mp3"), ("tut", None))
        self.assertEqual(classer_audio("Piano seul.mp3"), ("acc", None))
        self.assertEqual(classer_audio("Accompagnement.mp3"), ("acc", None))

    def test_double_voix_reste_a_part(self):
        self.assertEqual(classer_audio("Sopranos et altos.mp3"), ("autre", None))

    def test_nom_affiche_whatsapp(self):
        self.assertEqual(nom_affiche("AUD-20261115-WA0001.opus"), "Audio du 15/11")
        self.assertEqual(nom_affiche("Echauffement_voix.mp3"), "Echauffement voix")

    def test_copie_de_navigateur(self):
        self.assertEqual(classer_audio("Altos (1).mp3"), ("alt", None))


class Types(unittest.TestCase):
    def test_types(self):
        f = lambda nom, mime="": {"nom": nom, "mime": mime}
        self.assertEqual(type_fichier(f("Basses.mpeg", "video/mpeg")), "audio")
        self.assertEqual(type_fichier(f("Altos.mpeg", "audio/mpeg")), "audio")
        self.assertEqual(type_fichier(f("Répétition", "video/mp4")), "audio")
        self.assertEqual(type_fichier(f("Partition.pdf")), "pdf")
        self.assertEqual(type_fichier(f("Page 1.HEIC")), "image")
        self.assertEqual(type_fichier(f("Notes", "application/vnd.google-apps.document")), "google")
        self.assertIsNone(type_fichier(f("notes.docx")))
        self.assertEqual(type_fichier(f("Calage", "application/vnd.google-apps.document")), "calage")
        self.assertEqual(type_fichier(f("calage.txt")), "calage")


class Calage(unittest.TestCase):
    def test_lecture(self):
        texte = "Repères du chœur infernal\nSopranos : 0:12,4 ; 1:05,0 ; 2:38,0\nAltos: 7,8 ; 58.9 ; 2:29,5\nBasses 2 : 0:09 ; 1:00 ; 2:30\n"
        self.assertEqual(lire_calage(texte), [("Sopranos", [12.4, 65.0, 158.0]), ("Altos", [7.8, 58.9, 149.5]),
                                              ("Basses 2", [9.0, 60.0, 150.0])])

    def test_ecriture(self):
        self.assertEqual(ecrire_temps(158.04), "2:38,0")
        self.assertEqual(ecrire_temps(7.85), "0:07,8")

    def test_positions_communes(self):
        commun = plan_de_calage([[10, 110], [5, 95], [0, 105]])
        self.assertEqual(commun, [0, 100])
        self.assertIsNone(souci_de_calage([5, 95], commun))
        self.assertIn("plus lente", souci_de_calage([0, 140], commun))


class Dossiers(unittest.TestCase):
    def test_oeuvre_titre(self):
        self.assertEqual(lire_nom_dossier("Orphée et Eurydice – Chœur infernal"), (None, "Orphée et Eurydice", "Chœur infernal"))
        self.assertEqual(lire_nom_dossier("Fauré - Cantique de Jean Racine"), (None, "Fauré", "Cantique de Jean Racine"))

    def test_numero_d_ordre(self):
        self.assertEqual(lire_nom_dossier("01 – Gluck – Orphée – Chœur infernal"), (1, "Gluck · Orphée", "Chœur infernal"))
        self.assertEqual(lire_nom_dossier("3. Ave verum"), (3, "", "Ave verum"))

    def test_titre_seul_et_trait_d_union(self):
        self.assertEqual(lire_nom_dossier("Ave verum corpus"), (None, "", "Ave verum corpus"))
        self.assertEqual(lire_nom_dossier("Cantique de Jean-Racine"), (None, "", "Cantique de Jean-Racine"))
        self.assertEqual(lire_nom_dossier("1789 – Les amants"), (None, "1789", "Les amants"))


class Doublons(unittest.TestCase):
    def test_le_plus_recent_gagne(self):
        f = lambda nom, date: {"nom": nom, "chemin": nom, "modifie": date, "mime": ""}
        garde = garder_le_plus_recent([f("Altos.mp3", "2026-11-01"), f("Altos.m4a", "2026-11-15"), f("Basses.mp3", "2026-11-01")])
        self.assertEqual(sorted(x["nom"] for x in garde), ["Altos.m4a", "Basses.mp3"])


if __name__ == "__main__":
    unittest.main()
