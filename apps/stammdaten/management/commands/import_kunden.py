import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

import xlrd
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.kern.models import Mandant
from apps.stammdaten.models import (Ansprechpartner, Kunde, KundeMandant, Kundenart, Zahlungsbedingung)

KUNDENART = {"EK": Kundenart.ENDKUNDE, "GK": Kundenart.GEWERBLICH, "GH": Kundenart.WIEDERVERKAEUFER}
ANREDE = {1: "Herr", 2: "Frau"}
LAENDER = {"": "DE", "deutschland": "DE", "germany": "DE"}


def text(wert):
    if isinstance(wert, float):
        return str(int(wert)) if wert == int(wert) else str(wert)
    return str(wert).strip()


def erste_adresse(wert):
    return next((a.strip() for a in re.split(r"[,;]", text(wert)) if a.strip()), "")


class Command(BaseCommand):
    help = ("Importiert Kunden aus der Mention-Tabelle. Je Kunden-Nr. ein Kunde, je Zeile (Lfd.Nr.) ein "
            "Ansprechpartner; Zeile mit der kleinsten Lfd.Nr. ist der Hauptkontakt.")

    def add_arguments(self, parser):
        parser.add_argument("datei")
        parser.add_argument("--mandant", default="OKAY", help="Mandant, dem das Kreditlimit zugeordnet wird")
        parser.add_argument("--nur-weitere-mails", action="store_true",
                            help="nur das Feld 'weitere E-Mail-Adressen' bei vorhandenen Kunden füllen, nichts sonst ändern")

    @transaction.atomic
    def handle(self, datei, mandant, nur_weitere_mails=False, **opts):
        mandant_obj = Mandant.objects.filter(kurzname=mandant).first()
        if mandant_obj is None:
            raise CommandError(f"Mandant '{mandant}' nicht gefunden")
        blatt = xlrd.open_workbook(datei, encoding_override="cp1252").sheet_by_index(0)
        kopf = [str(c).strip() for c in blatt.row_values(0)]

        def spalte(name, nr=0):
            treffer = [i for i, k in enumerate(kopf) if k == name]
            if len(treffer) <= nr:
                raise CommandError(f"Spalte '{name}' fehlt")
            return treffer[nr]

        c = {n: spalte(n) for n in (
            "Kunden-Nr.", "Name 1", "Name 2", "Name 3", "Strasse", "PLZ", "Ort", "Land", "Bemerkung", "Hinweis",
            "E-Mail", "Telefon 1", "Eingabedatum", "Preis (1-6)", "Limit", "Umsatzsteuer-ID", "ZB 1", "Lfd.Nr.",
            "Online", "IBAN", "SWIFT", "Bank", "Kategorie")}
        c["anrede"] = spalte("Titel", 1)  # zweite Spalte "Titel": 1 = Herr, 2 = Frau

        gruppen = defaultdict(list)
        for i in range(1, blatt.nrows):
            z = blatt.row_values(i)
            if z[c["Kunden-Nr."]] != "":
                gruppen[text(z[c["Kunden-Nr."]])].append(z)

        zb = {z.nummer: z for z in Zahlungsbedingung.objects.exclude(nummer=None)}
        neu = ansprechpartner = 0
        mehrere_mails, telefon2, steuernummer, ohne_zb = [], [], [], []
        for nummer, zeilen in gruppen.items():
            zeilen.sort(key=lambda z: z[c["Lfd.Nr."]])
            k = zeilen[0]

            def erster_wert(name):
                return next((text(z[c[name]]) for z in zeilen if text(z[c[name]])), "")

            firma = text(k[c["Name 2"]]) or text(k[c["Name 1"]])
            land = LAENDER.get(text(k[c["Land"]]).lower(), text(k[c["Land"]])[:2].upper())
            zb_nr = int(k[c["ZB 1"]] or 0)
            if zb_nr and zb_nr not in zb:
                ohne_zb.append(f"{nummer}: ZB {zb_nr}")
            hinweis = "\n".join(t for t in (text(k[c["Bemerkung"]]), text(k[c["Hinweis"]])) if t)
            haupt = erste_adresse(k[c["E-Mail"]])
            weitere = list(dict.fromkeys(
                a.strip() for z in zeilen for a in re.split(r"[,;]", text(z[c["E-Mail"]]))
                if a.strip() and a.strip().lower() != haupt.lower()))
            if nur_weitere_mails:
                Kunde.objects.filter(nummer=nummer).update(email_weitere=", ".join(weitere))
                continue
            kunde, angelegt = Kunde.objects.update_or_create(nummer=nummer, defaults={
                "email_weitere": ", ".join(weitere),
                "name1": firma, "name2": text(k[c["Name 3"]]),
                "strasse": text(k[c["Strasse"]]), "plz": text(k[c["PLZ"]]), "ort": text(k[c["Ort"]]),
                "land": land or "DE", "telefon": text(k[c["Telefon 1"]]), "email": erste_adresse(k[c["E-Mail"]]),
                "ust_id": erster_wert("Umsatzsteuer-ID"),
                "kundenart": KUNDENART.get(text(k[c["Kategorie"]]), Kundenart.ENDKUNDE),
                "preisstufe": min(max(int(k[c["Preis (1-6)"]] or 1), 1), 3),
                "zahlungsbedingung": zb.get(zb_nr),
                "iban": erster_wert("IBAN"), "bic": erster_wert("SWIFT"), "bank": erster_wert("Bank"),
                "kontoinhaber": firma if erster_wert("IBAN") else "",
                "hinweis": hinweis,
            })
            neu += angelegt
            if isinstance(k[c["Eingabedatum"]], float) and k[c["Eingabedatum"]] > 0:
                Kunde.objects.filter(pk=kunde.pk).update(
                    erstkontakt=date(1899, 12, 30) + timedelta(days=int(k[c["Eingabedatum"]])))
            limit = k[c["Limit"]]
            if limit != "":
                KundeMandant.objects.update_or_create(
                    kunde=kunde, mandant=mandant_obj, defaults={"kreditlimit": Decimal(str(limit))})

            for z in zeilen:
                name = text(z[c["Name 1"]]) or text(z[c["Name 3"]])
                if not name and not text(z[c["E-Mail"]]):
                    continue
                Ansprechpartner.objects.update_or_create(
                    kunde=kunde, nachname=name or "Ansprechpartner", defaults={
                        "anrede": ANREDE.get(int(z[c["anrede"]] or 0), ""),
                        "email": erste_adresse(z[c["E-Mail"]]), "telefon": text(z[c["Telefon 1"]]),
                        "hauptkontakt": z is k, "shop_zugang": z[c["Online"]] == 1.0})
                ansprechpartner += 1
                if len([a for a in re.split(r"[,;]", text(z[c["E-Mail"]])) if a.strip()]) > 1:
                    mehrere_mails.append(f"{nummer}: {text(z[c['E-Mail']])}")
            telefon2.extend(nummer for z in zeilen if text(z[kopf.index("Telefon 2")]))
            steuernummer.extend(nummer for z in zeilen if text(z[kopf.index("Steuernummer")]))

        if nur_weitere_mails:
            self.stdout.write(f"E-Mail-Adressen bei {len(gruppen)} Kunden aktualisiert")
            return
        self.stdout.write(f"{len(gruppen)} Kunden ({neu} neu), {Ansprechpartner.objects.filter(kunde__nummer__in=list(gruppen)).count()} Ansprechpartner aus {blatt.nrows - 1} Zeilen")
        for titel, liste in (("Zahlungsbedingung nicht gefunden", ohne_zb),
                             ("Mehrere E-Mail-Adressen (erste als Hauptadresse, übrige unter weitere E-Mail-Adressen)", mehrere_mails),
                             ("Telefon 2 nicht übernommen bei Kunde", sorted(set(telefon2))),
                             ("Steuernummer nicht übernommen bei Kunde", sorted(set(steuernummer)))):
            if liste:
                self.stdout.write(f"{titel}: " + "; ".join(liste))
