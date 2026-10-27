"""
Verkauf: Belege (Angebot -> Auftrag -> Lieferschein -> Rechnung, Gutschrift) - Pflichtenheft Abschnitt 5.

Belege und Nummernkreise sind pro Mandant getrennt. Abgeschlossene Rechnungen und
Gutschriften sind gesperrt (GoBD); Korrektur nur per Storno/Gutschrift.
"""
import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from apps.kern.models import Mandant, Zeitstempel
from apps.stammdaten.models import Ansprechpartner, Artikel, Artikeltyp, Kunde, Zahlungsbedingung

CENT = Decimal("0.01")


def runden(wert: Decimal) -> Decimal:
    return wert.quantize(CENT, rounding=ROUND_HALF_UP)


class Belegart(models.TextChoices):
    ANGEBOT = "AN", "Angebot"
    AUFTRAG = "AB", "Auftrag"
    LIEFERSCHEIN = "LS", "Lieferschein"
    RECHNUNG = "RE", "Rechnung"
    NACHLIEFERSCHEIN = "NL", "Nachlieferschein"
    PROFORMA = "PF", "Proforma-Rechnung"
    GUTSCHRIFT = "GS", "Gutschrift"


# Nächste Stufe im Belegfluss ("per Klick")
FOLGEBELEG = {
    Belegart.ANGEBOT: Belegart.AUFTRAG,
    Belegart.AUFTRAG: Belegart.LIEFERSCHEIN,
    Belegart.LIEFERSCHEIN: Belegart.RECHNUNG,
}
# Diese Belegarten werden erst beim Abschluss nummeriert und danach gesperrt.
GESPERRT_NACH_ABSCHLUSS = {Belegart.RECHNUNG, Belegart.GUTSCHRIFT}


class BelegGesperrt(Exception):
    pass


class Nummernkreis(models.Model):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT)
    art = models.CharField("Belegart", max_length=2, choices=Belegart.choices)
    jahr = models.PositiveSmallIntegerField("Jahr")
    letzte_nummer = models.PositiveIntegerField("zuletzt vergeben", default=0)

    class Meta:
        verbose_name = "Nummernkreis"
        verbose_name_plural = "Nummernkreise"
        constraints = [models.UniqueConstraint(fields=["mandant", "art", "jahr"], name="nummernkreis_eindeutig")]

    @classmethod
    def naechste(cls, mandant: Mandant, art: str, jahr: int) -> str:
        with transaction.atomic():
            kreis, _ = cls.objects.select_for_update().get_or_create(mandant=mandant, art=art, jahr=jahr)
            kreis.letzte_nummer += 1
            kreis.save(update_fields=["letzte_nummer"])
        return f"{art}{jahr}-{kreis.letzte_nummer:05d}"

    @classmethod
    def freigeben(cls, mandant: Mandant, art: str, nummer: str):
        """Gibt die zuletzt vergebene Nummer wieder frei (nur wenn sie noch die letzte ist), damit keine Lücke entsteht."""
        try:
            jahr, zaehler = nummer[len(art):].split("-")
            jahr, zaehler = int(jahr), int(zaehler)
        except ValueError:
            return
        with transaction.atomic():
            kreis = cls.objects.select_for_update().filter(mandant=mandant, art=art, jahr=jahr).first()
            if kreis and kreis.letzte_nummer == zaehler:
                kreis.letzte_nummer -= 1
                kreis.save(update_fields=["letzte_nummer"])

    def __str__(self):
        return f"{self.mandant} {self.art} {self.jahr}: {self.letzte_nummer}"


class Beleg(Zeitstempel):
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="belege")
    art = models.CharField("Belegart", max_length=2, choices=Belegart.choices)
    nummer = models.CharField("Belegnummer", max_length=20, null=True, blank=True, default=None)
    kunde = models.ForeignKey(Kunde, on_delete=models.PROTECT, related_name="belege")
    ansprechpartner = models.ForeignKey(Ansprechpartner, on_delete=models.SET_NULL, null=True, blank=True)
    belegdatum = models.DateField("Belegdatum", default=timezone.localdate)
    zahlungsbedingung = models.ForeignKey(Zahlungsbedingung, on_delete=models.PROTECT, null=True, blank=True)
    rechnungsanschrift = models.TextField("Rechnungsanschrift", blank=True)
    versandanschrift = models.TextField("Versandanschrift", blank=True)
    versandart = models.CharField("Versandart", max_length=100, blank=True)
    versandkosten = models.DecimalField("Versandkosten netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    verpackung = models.DecimalField("Verpackung netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    versicherung = models.DecimalField("Versicherung netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    referenz = models.CharField("Ihre Referenz / Bestellnummer", max_length=100, blank=True)
    vortext = models.TextField("Text vor den Positionen", blank=True)
    schlusstext = models.TextField("Text nach den Positionen", blank=True)
    bemerkung = models.TextField("Bemerkung (intern, wird nicht gedruckt)", blank=True)
    vorgaenger = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="folgebelege")
    lieferscheine = models.ManyToManyField("self", symmetrical=False, blank=True, related_name="rechnungen",
                                           help_text="Bei Sammelrechnungen: alle Lieferscheine, die abgerechnet werden")
    abgeschlossen = models.BooleanField("abgeschlossen", default=False, editable=False)
    nl_lager_abbuchen = models.BooleanField(
        "Nachlieferschein: Ware vom Lager abbuchen", null=True, blank=True, editable=False,
        help_text="Leer = noch nicht beantwortet. Ja bei Ersatzlieferung (z. B. defektes Gerät), nein bei bloßer Nachlieferung.")
    summe_netto = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"), editable=False)
    summe_steuer = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"), editable=False)
    summe_brutto = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"), editable=False)

    class Meta:
        verbose_name = "Beleg"
        verbose_name_plural = "Belege"
        ordering = ["-belegdatum", "-id"]
        # Entwürfe haben keine Nummer (NULL) und zählen nicht als Duplikat
        constraints = [models.UniqueConstraint(fields=["mandant", "art", "nummer"], name="belegnummer_eindeutig")]

    def __str__(self):
        return f"{self.get_art_display()} {self.nummer or '(Entwurf)'}"

    @property
    def gesperrt(self) -> bool:
        return self.abgeschlossen and self.art in GESPERRT_NACH_ABSCHLUSS

    def loeschhindernis(self):
        """Grund, warum der Beleg nicht gelöscht werden darf (None = löschbar).
        Löschbar sind Angebote, Kundenbestellungen (Aufträge, auch nach dem Übernehmen in einen Folgebeleg) und noch nicht
        nummerierte Rechnungs-/Gutschriftentwürfe."""
        if self.art not in (Belegart.ANGEBOT, Belegart.AUFTRAG, Belegart.PROFORMA) and not (
                self.art in GESPERRT_NACH_ABSCHLUSS and not self.abgeschlossen and not self.nummer):
            return f"{self.get_art_display()} kann nicht gelöscht werden – Korrektur nur per Storno/Gutschrift."
        if self.rechnungen.exists():  # nur bei Lieferscheinen möglich; Angebote/Aufträge dürfen nach dem Übernehmen gelöscht werden
            return "Der Beleg wird in einer Rechnung abgerechnet – bitte zuerst diese entfernen."
        if self.mails.exists():
            return "Der Beleg wurde per E-Mail versendet; der Versandnachweis muss erhalten bleiben."
        if self.zahlungen.exists():
            return "Zum Beleg gibt es Zahlungen (Anzahlung) – bitte zuerst stornieren."
        if self.lagerbewegungen.exists():
            return "Zum Beleg gibt es Lagerbuchungen."
        return None

    def p_positionen(self):
        """Positionen mit Preislistenartikel (Typ P) ohne eigenen Artikel: in Auftrag, Lieferschein und Rechnung gelten sie nur als Angebot."""
        return self.positionen.filter(preisliste_artikel__isnull=False, artikel__isnull=True)

    @property
    def p_hinweis(self) -> str:
        """Text, solange Auftrag/Lieferschein/Rechnung noch Preislistenartikel (P) enthält; leer = in Ordnung."""
        if self.art not in (Belegart.AUFTRAG, Belegart.LIEFERSCHEIN, Belegart.RECHNUNG) or self.gesperrt:
            return ""
        if self.art != Belegart.AUFTRAG and self.abgeschlossen:
            return ""
        if self.p_positionen().exists():
            return (f"{self.get_art_display()} enthält Preislistenartikel (P): diese Positionen gelten nur als Angebot. "
                    "Drucken, Abschließen und Versenden geht erst, wenn alle Positionen eigene Artikel (A) sind – "
                    "nach dem Wareneingang werden sie automatisch umgestellt.")
        return ""

    def positionen_pruefen(self):
        """Ein Beleg ohne Position darf nicht abgeschlossen, versendet oder weitergeführt werden."""
        if not self.positionen.exists():
            raise ValueError(f"{self} hat keine Position – erst Positionen erfassen.")

    @property
    def abrechenbar(self) -> bool:
        """Abgeschlossener Lieferschein, der noch in keine Rechnung (und keinen anderen Folgebeleg) übernommen wurde."""
        return (self.art == Belegart.LIEFERSCHEIN and self.abgeschlossen
                and not self.folgebelege.exists() and not self.rechnungen.exists())

    @property
    def folgeart(self):
        return FOLGEBELEG.get(self.art)

    @property
    def folgeart_name(self):
        return Belegart(self.folgeart).label if self.folgeart else ""

    def save(self, *args, **kwargs):
        if self.pk and self.gesperrt:
            vorher = Beleg.objects.get(pk=self.pk)
            # nur Versandart und Zahlungsbedingung bleiben änderbar
            for feld in ("kunde_id", "belegdatum", "versandkosten", "verpackung", "versicherung", "summe_netto", "summe_brutto", "art", "nummer"):
                if getattr(vorher, feld) != getattr(self, feld):
                    raise BelegGesperrt(f"{self}: Feld '{feld}' ist nach Abschluss gesperrt.")
        self.nummer = self.nummer or None
        if not self.pk and not self.nummer and self.art not in GESPERRT_NACH_ABSCHLUSS:
            self.nummer = Nummernkreis.naechste(self.mandant, self.art, self.belegdatum.year)
        super().save(*args, **kwargs)

    @classmethod
    def leere_entwuerfe_entfernen(cls, mandant, kunde=None, minuten=2):
        """Belege ohne Position werden nicht aufbewahrt: leere, nicht abgeschlossene Entwürfe, die älter als ein paar Minuten
        sind und an denen nichts hängt, werden gelöscht; ihre Nummer wird wieder frei, wenn sie die letzte vergebene war."""
        grenze = timezone.now() - timedelta(minutes=minuten)
        kandidaten = cls.objects.filter(mandant=mandant, abgeschlossen=False, angelegt_am__lt=grenze, positionen__isnull=True)
        if kunde is not None:
            kandidaten = kandidaten.filter(kunde=kunde)
        anzahl = 0
        for b in kandidaten.order_by("-id"):
            if b.folgebelege.exists() or b.rechnungen.exists() or b.mails.exists() or b.zahlungen.exists() \
                    or b.lagerbewegungen.exists():
                continue
            nummer, art = b.nummer, b.art
            b.delete()
            if nummer:
                Nummernkreis.freigeben(mandant, art, nummer)
            anzahl += 1
        return anzahl

    def ek_und_marge(self):
        """(EK netto, Marge in % vom Netto-VK, unvollständig) für die interne Anzeige; None bei Belegarten ohne Anzeige.
        EK je Position: Lagerartikel/Gruppen über Wareneingang bzw. günstigste Preisliste, Preislistenartikel mit ihrem EK;
        Gruppen zählen über ihre Unterpositionen. Leistungen und freie Positionen haben keinen EK (unvollständig = True)."""
        if self.art not in (Belegart.ANGEBOT, Belegart.AUFTRAG, Belegart.RECHNUNG, Belegart.PROFORMA):
            return None
        ek, vk, luecke = Decimal("0"), Decimal("0"), False
        positionen = list(self.positionen.select_related("artikel", "preisliste_artikel"))
        teile = {}
        for p in positionen:
            if p.gruppe_id:
                teile.setdefault(p.gruppe_id, []).append(p)

        def einzel_ek(p):
            if p.preisliste_artikel_id:
                return p.preisliste_artikel.ek
            if p.artikel_id and p.artikel.typ in ("A", "G"):
                return p.artikel.ek_errechnet
            return None

        for p in positionen:
            if p.gruppe_id:
                continue
            vk += p.netto_gesamt
            quellen = teile.get(p.pk) or [p]
            for q in quellen:
                wert = einzel_ek(q)
                if wert is None:
                    luecke = True
                else:
                    ek += wert * q.menge
        marge = ((vk - ek) / vk * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if vk else Decimal("0")
        return ek.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), marge, luecke

    @property
    def ek_marge_code(self) -> str:
        """EK und Marge hintereinander ohne Trennzeichen (z. B. 400,50 und 15,25 → 400,5015,25): für den Verkäufer lesbar, für Dritte nicht."""
        daten = self.ek_und_marge()
        if daten is None:
            return ""
        ek, marge, luecke = daten
        return f"{ek:.2f}{marge:.2f}".replace(".", ",") + ("*" if luecke else "")

    def gruppenpreise_aktualisieren(self):
        """Der Preis einer Gruppe (Typ G) ist die Summe ihrer Unterpositionen, solange er nicht von Hand festgelegt wurde."""
        for kopf in Position.objects.filter(beleg=self, typ="G", gruppe__isnull=True, preis_fest=False):
            teile = list(kopf.unterpositionen.all())
            if not teile:
                continue
            summe = sum((u.menge * u.einzelpreis for u in teile), Decimal("0"))
            preis = (summe / kopf.menge).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if kopf.menge else Decimal("0")
            if preis != kopf.einzelpreis:
                Position.objects.filter(pk=kopf.pk).update(einzelpreis=preis)

    def positionen_nummerieren(self):
        """Positionsnummern lückenlos 1, 2, 3 …; Unterpositionen einer Gruppe tragen die Nummer ihrer Gruppe."""
        alle = list(Position.objects.filter(beleg=self).order_by("pos", "id"))
        nummer = {}
        kopfnr = 0
        for p in alle:
            if not p.gruppe_id:
                kopfnr += 1
                nummer[p.pk] = kopfnr
        for p in alle:
            if p.gruppe_id:
                nummer[p.pk] = nummer.get(p.gruppe_id, kopfnr)
        # Reihenfolge: Gruppe, dann ihre Unterpositionen (nach id)
        for p in alle:
            if p.pos != nummer[p.pk]:
                Position.objects.filter(pk=p.pk).update(pos=nummer[p.pk])

    def berechne(self):
        """Summen aus Positionen; Versandkosten laufen mit dem höchsten Steuersatz der Positionen."""
        if self.gesperrt:
            raise BelegGesperrt(f"{self} ist gesperrt.")
        self.positionen_nummerieren()
        self.gruppenpreise_aktualisieren()
        netto = Decimal("0")
        saetze = {}
        for p in self.positionen.all():
            n = p.netto_gesamt
            netto += n
            saetze[p.steuer_prozent] = saetze.get(p.steuer_prozent, Decimal("0")) + n
        nebenkosten = self.versandkosten + self.verpackung + self.versicherung
        if nebenkosten:
            satz = max(saetze, default=Decimal("19"))
            saetze[satz] = saetze.get(satz, Decimal("0")) + nebenkosten
            netto += nebenkosten
        steuer = sum((runden(basis * satz / 100) for satz, basis in saetze.items()), Decimal("0"))
        self.summe_netto, self.summe_steuer = runden(netto), steuer
        self.summe_brutto = self.summe_netto + steuer
        Beleg.objects.filter(pk=self.pk).update(
            summe_netto=self.summe_netto, summe_steuer=self.summe_steuer, summe_brutto=self.summe_brutto)

    @transaction.atomic
    def abschliessen(self):
        if self.abgeschlossen:
            return
        if not self.positionen.exists():
            raise ValueError("Beleg ohne Positionen kann nicht abgeschlossen werden.")
        if self.art in (Belegart.AUFTRAG, Belegart.LIEFERSCHEIN, Belegart.RECHNUNG) and self.p_positionen().exists():
            raise ValueError(f"{self.get_art_display()} enthält Preislistenartikel (P) – nur als Angebot möglich. "
                             "Erst Wareneingang buchen, dann sind es eigene Artikel (A).")
        if self.art == Belegart.RECHNUNG:  # Die Ware muss über den Lieferschein abgebucht sein, sonst bliebe der Bestand falsch
            from apps.lager.services import offene_lieferscheine
            offen = offene_lieferscheine(self)
            if offen:
                raise ValueError("Erst den Lieferschein " + ", ".join(l.nummer or "(Entwurf)" for l in offen) +
                                 " abschließen (bucht das Lager ab), dann die Rechnung.")
        self.berechne()
        if not self.nummer:
            self.nummer = Nummernkreis.naechste(self.mandant, self.art, self.belegdatum.year)
        self.abgeschlossen = True
        Beleg.objects.filter(pk=self.pk).update(nummer=self.nummer, abgeschlossen=True)
        from apps.lager.services import buche_beleg  # Lieferschein/Rechnung buchen den Bestand ab
        from apps.kern.middleware import aktueller_benutzer
        benutzer = aktueller_benutzer()
        benutzer = benutzer if benutzer is not None and benutzer.is_authenticated else None
        buche_beleg(self, benutzer)
        from apps.esd.services import beleg_zuordnen  # ESD-Keys beim Verkauf automatisch zuordnen
        beleg_zuordnen(self, benutzer)

    @transaction.atomic
    def folgebeleg_erzeugen(self) -> "Beleg":
        art = self.folgeart
        if not art:
            raise ValueError("Für diese Belegart gibt es keinen Folgebeleg.")
        if self.art == Belegart.LIEFERSCHEIN and not self.abgeschlossen:
            raise ValueError("Den Lieferschein erst abschließen (bucht das Lager ab), dann die Rechnung daraus erstellen.")
        self.positionen_pruefen()
        neu = Beleg.objects.create(
            mandant=self.mandant, art=art, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=self.zahlungsbedingung, rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=self.versandkosten, verpackung=self.verpackung, versicherung=self.versicherung,
            referenz=self.referenz, vortext=self.vortext, schlusstext=self.schlusstext,
            bemerkung=self.bemerkung, vorgaenger=self)
        kopiere_positionen(self, neu)
        if self.art == Belegart.LIEFERSCHEIN:
            neu.lieferscheine.add(self)
        neu.berechne()
        return neu


    @transaction.atomic
    def teillieferung_abschliessen(self) -> "Beleg":
        """Positionen mit ausreichendem Bestand wandern in einen neuen Beleg derselben Art, der abgeschlossen wird;
        der Rest bleibt unverändert als Entwurf in diesem Beleg. Versandkosten laufen mit der Teillieferung."""
        from apps.lager.services import teilung
        lieferbar, knapp = teilung(self)
        if not lieferbar or not knapp:
            raise ValueError("Eine Teillieferung ist nur möglich, wenn ein Teil der Positionen lieferbar ist und ein Teil nicht.")
        neu = Beleg.objects.create(
            mandant=self.mandant, art=self.art, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=self.zahlungsbedingung, rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=self.versandkosten, verpackung=self.verpackung, versicherung=self.versicherung,
            referenz=self.referenz, vortext=self.vortext, schlusstext=self.schlusstext,
            bemerkung=self.bemerkung, vorgaenger=self.vorgaenger, nl_lager_abbuchen=self.nl_lager_abbuchen)
        ids = [p.pk for p in lieferbar]
        Position.objects.filter(models.Q(pk__in=ids) | models.Q(gruppe_id__in=ids)).update(beleg=neu)
        Beleg.objects.filter(pk=self.pk).update(versandkosten=0, verpackung=0, versicherung=0)
        self.versandkosten = self.verpackung = self.versicherung = Decimal("0")
        self.berechne()
        neu.berechne()
        neu.abschliessen()
        return neu

    @transaction.atomic
    def kopieren_als(self, art, urbeleg_abschliessen=False) -> "Beleg":
        """Beliebigen Beleg als neuen Entwurf der gewünschten Belegart kopieren (Gutschrift nur über Storno).
        Optional wird der Ursprungsbeleg danach abgeschlossen (erledigt)."""
        if art not in Belegart.values or art == Belegart.GUTSCHRIFT:
            raise ValueError("Diese Belegart kann nicht als Kopie angelegt werden.")
        if art == Belegart.LIEFERSCHEIN and self.art != Belegart.AUFTRAG:
            raise ValueError("Ein Lieferschein entsteht nur aus einem Auftrag.")
        if art == Belegart.NACHLIEFERSCHEIN and (self.art != Belegart.RECHNUNG or not self.abgeschlossen):
            raise ValueError("Ein Nachlieferschein entsteht nur aus einer abgeschlossenen Rechnung.")
        if self.art == Belegart.LIEFERSCHEIN and art == Belegart.RECHNUNG and not self.abgeschlossen:
            raise ValueError("Den Lieferschein erst abschließen (bucht das Lager ab), dann die Rechnung daraus erstellen.")
        self.positionen_pruefen()
        nachlieferung = art == Belegart.NACHLIEFERSCHEIN
        neu = Beleg.objects.create(
            mandant=self.mandant, art=art, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=zahlungsbedingung_bezahlt() if nachlieferung else self.zahlungsbedingung,
            rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=0 if nachlieferung else self.versandkosten, verpackung=0 if nachlieferung else self.verpackung,
            versicherung=0 if nachlieferung else self.versicherung,
            referenz=self.referenz, vortext=self.vortext, schlusstext=self.schlusstext,
            bemerkung=f"Nachlieferung zu Rechnung {self.nummer}" if nachlieferung else self.bemerkung,
            vorgaenger=self if art != self.art else None)
        kopiere_positionen(self, neu)
        if self.art == Belegart.LIEFERSCHEIN and art == Belegart.RECHNUNG:
            neu.lieferscheine.add(self)
        neu.berechne()
        if urbeleg_abschliessen and not self.abgeschlossen:
            self.abschliessen()
        return neu

    @transaction.atomic
    def gutschrift_erzeugen(self) -> "Beleg":
        """Storno/Gutschrift zu einer abgeschlossenen Rechnung (Positionen unverändert, Belegart Gutschrift)."""
        if self.art != Belegart.RECHNUNG or not self.abgeschlossen:
            raise ValueError("Eine Gutschrift gibt es nur zu einer abgeschlossenen Rechnung.")
        neu = Beleg.objects.create(
            mandant=self.mandant, art=Belegart.GUTSCHRIFT, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=self.zahlungsbedingung, rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=self.versandkosten, verpackung=self.verpackung, versicherung=self.versicherung,
            referenz=self.referenz, bemerkung=f"Gutschrift zu Rechnung {self.nummer}", vorgaenger=self)
        kopiere_positionen(self, neu)
        neu.berechne()
        return neu

    @property
    def faellig_am(self):
        """Belegdatum + Tage der Zahlungsbedingung (Rechnung fällig)."""
        if not self.zahlungsbedingung_id:
            return None
        return self.belegdatum + timedelta(days=self.zahlungsbedingung.tage_netto)

    @property
    def zahlungstext(self) -> str:
        """Text der Zahlungsbedingung; ff.ff.ffff wird durch das Fälligkeitsdatum ersetzt."""
        zb = self.zahlungsbedingung
        if not zb:
            return ""
        return (zb.druck_text or zb.bezeichnung).replace("ff.ff.ffff", self.faellig_am.strftime("%d.%m.%Y"))


class Mailvorlage(Zeitstempel):
    """Betreff und Text je Mandant und Belegart. Platzhalter: {anrede} {art} {nummer} {firma} {kunde} {datum}
    {faelligkeit} {zahlungstext} {betrag}"""

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT)
    art = models.CharField("Belegart", max_length=2, choices=Belegart.choices)
    betreff = models.CharField("Betreff", max_length=200)
    text = models.TextField("Text")

    class Meta:
        verbose_name = "Mailvorlage"
        verbose_name_plural = "Mailvorlagen"
        constraints = [models.UniqueConstraint(fields=["mandant", "art"], name="mailvorlage_eindeutig")]

    def __str__(self):
        return f"{self.mandant} – {self.get_art_display()}"


class MailVersand(models.Model):
    """Unveränderlicher Nachweis, wann welcher Beleg an wen verschickt wurde."""

    beleg = models.ForeignKey(Beleg, on_delete=models.PROTECT, related_name="mails", null=True, blank=True)
    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    bezug = models.CharField("Bezug", max_length=100, blank=True, help_text="z. B. Reparatur oder Bestellung")
    zeitpunkt = models.DateTimeField(default=timezone.now, db_index=True)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    an = models.CharField("An", max_length=500)
    betreff = models.CharField("Betreff", max_length=200)
    text = models.TextField("Text")
    erfolgreich = models.BooleanField(default=False)
    fehler = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = "Mailversand"
        verbose_name_plural = "Mailversand"
        ordering = ["-zeitpunkt"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Versandprotokoll ist unveränderlich.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Versandprotokoll ist unveränderlich.")

    def __str__(self):
        return f"{self.beleg or self.bezug} → {self.an} ({'ok' if self.erfolgreich else 'Fehler'})"


def zahlungsbedingung_bezahlt():
    """Zahlungsbedingung für Nachlieferscheine: die Ware ist mit der Rechnung bezahlt (oder wird es)."""
    from apps.stammdaten.models import Zahlungsbedingung
    zb, _ = Zahlungsbedingung.objects.get_or_create(
        bezeichnung="Betrag dankend erhalten", defaults={"druck_text": "Betrag dankend erhalten", "tage_netto": 0})
    return zb


def kopiere_positionen(quelle, ziel):
    """Kopiert alle Positionen samt Gruppenzuordnung in einen anderen Beleg. Stammt die Quelle aus einem
    Lieferschein, bleibt der Lieferschein an der Position vermerkt (Nummer und Datum im Ausdruck)."""
    neu = {}
    lieferschein = quelle if quelle.art == Belegart.LIEFERSCHEIN else None
    for p in quelle.positionen.order_by("pos", "id"):
        neu[p.pk] = Position.objects.create(
            beleg=ziel, gruppe=neu.get(p.gruppe_id), typ=p.typ, artikel=p.artikel, bezeichnung=p.bezeichnung,
            preisliste_artikel=p.preisliste_artikel, menge=p.menge, einzelpreis=p.einzelpreis, rabatt_prozent=p.rabatt_prozent,
            steuer_prozent=p.steuer_prozent, zusatztext=p.zusatztext, lieferschein=lieferschein or p.lieferschein,
            quellposition=p, preis_fest=p.preis_fest)


@transaction.atomic
def rechnung_aus_lieferscheinen(lieferscheine) -> Beleg:
    """Mehrere Lieferscheine eines Kunden zu einer Rechnung zusammenfassen."""
    lieferscheine = sorted(lieferscheine, key=lambda b: (b.belegdatum, b.pk))
    if not lieferscheine:
        raise ValueError("Keine Lieferscheine ausgewählt.")
    erster = lieferscheine[0]
    for ls in lieferscheine:
        if ls.mandant_id != erster.mandant_id or ls.kunde_id != erster.kunde_id:
            raise ValueError("Alle Lieferscheine müssen zum selben Kunden und Mandanten gehören.")
        if not ls.abrechenbar:
            raise ValueError(f"{ls} ist nicht abrechenbar (nicht abgeschlossen oder schon berechnet).")
    rechnung = Beleg.objects.create(
        mandant=erster.mandant, art=Belegart.RECHNUNG, kunde=erster.kunde, ansprechpartner=erster.ansprechpartner,
        zahlungsbedingung=erster.zahlungsbedingung or erster.kunde.zahlungsbedingung,
        rechnungsanschrift=erster.rechnungsanschrift, versandanschrift=erster.versandanschrift,
        versandart=erster.versandart, vorgaenger=erster,
        bemerkung="Lieferscheine: " + ", ".join(ls.nummer for ls in lieferscheine))
    for ls in lieferscheine:
        kopiere_positionen(ls, rechnung)
    rechnung.lieferscheine.set(lieferscheine)
    rechnung.versandkosten = sum((ls.versandkosten for ls in lieferscheine), Decimal("0"))
    rechnung.verpackung = sum((ls.verpackung for ls in lieferscheine), Decimal("0"))
    rechnung.versicherung = sum((ls.versicherung for ls in lieferscheine), Decimal("0"))
    rechnung.save()
    rechnung.berechne()
    return rechnung


class Position(Zeitstempel):
    beleg = models.ForeignKey(Beleg, on_delete=models.CASCADE, related_name="positionen")
    lieferschein = models.ForeignKey(Beleg, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                     help_text="Lieferschein, aus dem die Position übernommen wurde")
    quellposition = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    gruppe = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="unterpositionen",
                               help_text="Gesetzt bei Unterpositionen einer Gruppe (Typ G); sie haben keinen eigenen Preis.")
    pos = models.PositiveSmallIntegerField("Pos.", default=0)
    typ = models.CharField("Typ", max_length=1, choices=Artikeltyp.choices, default=Artikeltyp.A)
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT, null=True, blank=True)
    preisliste_artikel = models.ForeignKey("stammdaten.Preislistenartikel", on_delete=models.SET_NULL, null=True, blank=True,
                                           related_name="+", help_text="Typ P: Artikel aus der Lieferantenpreisliste ohne eigene Artikelanlage")
    bezeichnung = models.CharField("Bezeichnung", max_length=255)
    zusatztext = models.TextField("Zusatztext", blank=True)
    menge = models.DecimalField("Menge", max_digits=10, decimal_places=3, default=Decimal("1"))
    einzelpreis = models.DecimalField("Einzelpreis netto", max_digits=12, decimal_places=2)
    rabatt_prozent = models.DecimalField("Rabatt %", max_digits=5, decimal_places=2, default=Decimal("0"))
    preis_fest = models.BooleanField("Gruppenpreis fest", default=False,
                                     help_text="Nur bei Gruppen (Typ G): ein von Hand eingetragener Preis wird nicht aus den Unterpositionen berechnet.")
    steuer_prozent = models.DecimalField("Steuer %", max_digits=5, decimal_places=2, default=Decimal("19"))

    class Meta:
        verbose_name = "Position"
        verbose_name_plural = "Positionen"
        ordering = ["pos", "id"]

    @property
    def artikelnummer(self) -> str:
        """Nummer für Anzeige und Druck: eigener Artikel, sonst MSKU/Bestellnummer aus der Preisliste."""
        if self.artikel_id:
            return self.artikel.nummer
        return self.preisliste_artikel.nummer if self.preisliste_artikel_id else ""

    @property
    def ek_stueck(self):
        """EK netto je Einheit der Position (Gruppen: Summe ihrer Unterpositionen je Gruppeneinheit); None = unbekannt."""
        if self.typ == "G" and not self.gruppe_id and self.pk:
            teile = list(self.unterpositionen.select_related("artikel", "preisliste_artikel"))
            if teile:
                summe = Decimal("0")
                for t in teile:
                    e = t.ek_stueck
                    if e is None:
                        return None
                    summe += e * t.menge
                return (summe / self.menge) if self.menge else None
        if self.preisliste_artikel_id:
            return self.preisliste_artikel.ek
        if self.artikel_id and self.artikel.typ in ("A", "G"):
            return self.artikel.ek_errechnet
        return None

    @property
    def netto_gesamt(self) -> Decimal:
        if self.gruppe_id:  # Unterartikel einer Gruppe: der Preis steht an der Gruppenposition
            return Decimal("0.00")
        return runden(self.menge * self.einzelpreis * (Decimal("100") - self.rabatt_prozent) / 100)

    def save(self, *args, **kwargs):
        if self.beleg.gesperrt:
            raise BelegGesperrt(f"{self.beleg} ist gesperrt.")
        if not self.pk and not self.pos:
            letzte = Position.objects.filter(beleg=self.beleg).aggregate(m=models.Max("pos"))["m"] or 0
            self.pos = letzte + 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.beleg.gesperrt:
            raise BelegGesperrt(f"{self.beleg} ist gesperrt.")
        super().delete(*args, **kwargs)


def monate_addieren(tag: date, monate: int) -> date:
    """Datum um ganze Monate verschieben; der Tag wird bei kurzen Monaten auf den Monatsletzten gekürzt."""
    index = tag.year * 12 + tag.month - 1 + monate
    jahr, monat = divmod(index, 12)
    return date(jahr, monat + 1, min(tag.day, calendar.monthrange(jahr, monat + 1)[1]))


class Abo(Zeitstempel):
    """Wiederkehrende Rechnung (Wartungsvertrag, Hosting, Miete …): erzeugt zum Termin einen Rechnungsentwurf."""

    INTERVALLE = [(1, "monatlich"), (3, "vierteljährlich"), (6, "halbjährlich"), (12, "jährlich")]

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="abos")
    kunde = models.ForeignKey(Kunde, on_delete=models.PROTECT, related_name="abos")
    bezeichnung = models.CharField("Bezeichnung", max_length=100, help_text="z. B. Wartungsvertrag 2026")
    monate = models.PositiveSmallIntegerField("Rhythmus", choices=INTERVALLE, default=1)
    naechster_termin = models.DateField("nächste Rechnung am")
    ende = models.DateField("Ende (optional)", null=True, blank=True)
    aktiv = models.BooleanField(default=True)
    zahlungsbedingung = models.ForeignKey(Zahlungsbedingung, on_delete=models.PROTECT, null=True, blank=True)
    vortext = models.TextField("Text vor den Positionen", blank=True)
    schlusstext = models.TextField("Text nach den Positionen", blank=True)
    letzter_lauf = models.DateField(null=True, blank=True, editable=False)
    letzte_rechnung = models.ForeignKey("Beleg", on_delete=models.SET_NULL, null=True, blank=True, editable=False, related_name="+")

    class Meta:
        verbose_name = "Wiederkehrende Rechnung"
        verbose_name_plural = "Wiederkehrende Rechnungen"
        ordering = ["naechster_termin", "kunde"]

    def __str__(self):
        return f"{self.bezeichnung} – {self.kunde.name1} ({self.get_monate_display()})"

    @property
    def netto(self):
        return sum((p.menge * p.einzelpreis * (1 - p.rabatt_prozent / 100) for p in self.positionen.all()), Decimal("0"))

    @transaction.atomic
    def rechnung_erzeugen(self, heute=None) -> "Beleg":
        """Legt den Rechnungsentwurf an (nummeriert und abgeschlossen wird von Hand), rückt den Termin weiter."""
        from apps.verkauf.views import adresse_text
        from apps.stammdaten.models import Anschriftart
        heute = heute or timezone.localdate()
        if not self.positionen.exists():
            raise ValueError(f"{self}: keine Positionen hinterlegt.")
        beleg = Beleg.objects.create(
            mandant=self.mandant, art=Belegart.RECHNUNG, kunde=self.kunde, belegdatum=heute,
            ansprechpartner=self.kunde.ansprechpartner.filter(hauptkontakt=True).first(),
            zahlungsbedingung=self.zahlungsbedingung or self.kunde.zahlungsbedingung,
            rechnungsanschrift=adresse_text(self.kunde, Anschriftart.RECHNUNG),
            versandanschrift=adresse_text(self.kunde, Anschriftart.VERSAND),
            vortext=self.vortext, schlusstext=self.schlusstext, bemerkung=f"Wiederkehrende Rechnung: {self.bezeichnung}")
        for p in self.positionen.all():
            Position.objects.create(
                beleg=beleg, typ=("A" if p.artikel_id else "L"), artikel=p.artikel, bezeichnung=p.bezeichnung, menge=p.menge,
                einzelpreis=p.einzelpreis, rabatt_prozent=p.rabatt_prozent, steuer_prozent=p.steuer_prozent)
        beleg.berechne()
        self.letzter_lauf, self.letzte_rechnung = heute, beleg
        self.naechster_termin = monate_addieren(self.naechster_termin, self.monate)
        if self.ende and self.naechster_termin > self.ende:
            self.aktiv = False
        self.save()
        return beleg


class AboPosition(models.Model):
    abo = models.ForeignKey(Abo, on_delete=models.CASCADE, related_name="positionen")
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT, null=True, blank=True)
    bezeichnung = models.CharField("Bezeichnung", max_length=255)
    menge = models.DecimalField("Menge", max_digits=10, decimal_places=3, default=Decimal("1"))
    einzelpreis = models.DecimalField("Einzelpreis netto", max_digits=12, decimal_places=2)
    rabatt_prozent = models.DecimalField("Rabatt %", max_digits=5, decimal_places=2, default=Decimal("0"))
    steuer_prozent = models.DecimalField("Steuer %", max_digits=5, decimal_places=2, default=Decimal("19"))

    class Meta:
        verbose_name = "Position"
        verbose_name_plural = "Positionen"
        ordering = ["id"]


def faellige_abos(heute=None):
    heute = heute or timezone.localdate()
    return Abo.objects.filter(aktiv=True, naechster_termin__lte=heute).select_related("kunde", "mandant")
