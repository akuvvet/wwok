import os
from decimal import Decimal
from pathlib import Path

import xlrd
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.stammdaten.models import Artikel, ArtikelBild, Artikeltyp, Hersteller, Kategorie, Steuersatz

SPALTEN = {
    "nummer": "Nummer", "herstellernummer": "Hersteller-Nr.", "bezeichnung": "Bezeichnung",
    "hersteller": "Hersteller-Name", "steuersatz": "Steuersatz", "ek": "EK-Preis", "online": "Online",
    "erweitert": "Erweiterte Benennung", "ean": "EAN-Code", "kategorie": "Kategorie", "inaktiv": "nicht aktiv",
    "vk1": "VK-Preis 1", "vk3": "VK-Preis 3", "meta_titel": "Meta Title Online",
    "keywords": "Keywords Online", "meta_description": "Meta Description Online",
}
BILDSPALTEN = ("Abbildung 1", "Abbildung 2", "Abbildung 3")


def text(wert):
    if isinstance(wert, float):
        return str(int(wert)) if wert == int(wert) else str(wert)
    return str(wert).strip()


def geld(wert):
    return Decimal(str(wert)).quantize(Decimal("0.01")) if wert != "" else None


class Command(BaseCommand):
    help = "Importiert Artikel aus der Mention-/Shopware-Tabelle. Bei doppelten Nummern gilt die erste Zeile."

    def add_arguments(self, parser):
        parser.add_argument("datei")
        parser.add_argument("--bilder", help="Ordner mit den Abbildungen (Zuordnung über den Dateinamen)")

    @transaction.atomic
    def handle(self, datei, bilder=None, **opts):
        blatt = xlrd.open_workbook(datei, encoding_override="cp1252").sheet_by_index(0)
        kopf = [str(c).strip() for c in blatt.row_values(0)]
        spalte = {}
        for schluessel, name in SPALTEN.items():
            if name not in kopf:
                raise CommandError(f"Spalte '{name}' fehlt")
            spalte[schluessel] = kopf.index(name)
        bildspalten = [kopf.index(n) for n in BILDSPALTEN if n in kopf]
        steuer = {s.nummer: s for s in Steuersatz.objects.exclude(nummer=None)}
        kategorien = {k.nummer: k for k in Kategorie.objects.exclude(nummer=None)}
        hersteller = {}
        for h in Hersteller.objects.order_by("kennzeichen"):
            hersteller.setdefault(h.name.lower(), h)
        ordner = Path(bilder) if bilder else None

        neu = aktualisiert = doppelt = bilder_neu = 0
        fehlende_bilder, ohne_hersteller, nicht_online = [], [], []
        gesehen = set()
        for i in range(1, blatt.nrows):
            z = blatt.row_values(i)
            nummer = text(z[spalte["nummer"]])
            if not nummer:
                continue
            if nummer in gesehen:
                doppelt += 1
                continue
            gesehen.add(nummer)
            satz = steuer.get(int(z[spalte["steuersatz"]]))
            if satz is None:
                raise CommandError(f"Artikel {nummer}: Steuersatz {z[spalte['steuersatz']]} unbekannt")
            kategorie = kategorien.get(text(z[spalte["kategorie"]]))
            if kategorie is None:
                raise CommandError(f"Artikel {nummer}: Kategorie {z[spalte['kategorie']]} unbekannt")
            name = text(z[spalte["hersteller"]])
            h = hersteller.get(name.lower()) if name else None
            if name and h is None:
                ohne_hersteller.append(f"{nummer}: {name}")
            online_wert = z[spalte["online"]]
            if online_wert not in (0.0, 1.0, ""):
                nicht_online.append(f"{nummer}: {online_wert}")
            artikel, angelegt = Artikel.objects.update_or_create(nummer=nummer, defaults={
                "typ": Artikeltyp.A,
                "bezeichnung": text(z[spalte["bezeichnung"]]),
                "bezeichnung_erweitert": text(z[spalte["erweitert"]]),
                "kategorie": kategorie, "hersteller": h,
                "herstellernummer": text(z[spalte["herstellernummer"]]),
                "ean": text(z[spalte["ean"]]),
                "steuersatz": satz,
                "durchschnitts_ek": Decimal(str(z[spalte["ek"]])).quantize(Decimal("0.0001")) if z[spalte["ek"]] != "" else None,
                "vk1": geld(z[spalte["vk1"]]), "vk3": geld(z[spalte["vk3"]]),
                "online": online_wert == 1.0,
                "nicht_aktiv": z[spalte["inaktiv"]] == 1.0,
                "meta_titel": text(z[spalte["meta_titel"]]),
                "keywords": text(z[spalte["keywords"]]),
                "meta_description": text(z[spalte["meta_description"]]),
            })
            neu += angelegt
            aktualisiert += not angelegt
            if ordner:
                for pos, sp in enumerate(bildspalten, start=1):
                    pfad = str(z[sp]).strip()
                    if not pfad or ArtikelBild.objects.filter(artikel=artikel, position=pos).exists():
                        continue
                    datei_bild = ordner / pfad.replace("\\", "/").split("/")[-1]
                    if not datei_bild.is_file():
                        fehlende_bilder.append(pfad)
                        continue
                    with open(datei_bild, "rb") as f:
                        ArtikelBild(artikel=artikel, position=pos).bild.save(datei_bild.name, File(f), save=True)
                    bilder_neu += 1

        self.stdout.write(f"{neu} neu, {aktualisiert} aktualisiert, {doppelt} doppelte Zeilen übersprungen, {bilder_neu} Bilder")
        for titel, liste in (("Hersteller nicht gefunden", ohne_hersteller),
                             ("Online-Wert ungleich 0/1 (als offline importiert)", nicht_online),
                             ("Bild nicht gefunden", fehlende_bilder)):
            if liste:
                self.stdout.write(f"{titel}: " + "; ".join(liste))
