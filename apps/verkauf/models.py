"""
Verkauf: Belege (Angebot -> Bestellung -> Lieferschein -> Rechnung, Gutschrift) - Pflichtenheft Abschnitt 5.

Belege und Nummernkreise sind pro Mandant getrennt. Abgeschlossene Rechnungen und
Gutschriften sind gesperrt (GoBD); Korrektur nur per Storno/Gutschrift.
"""
import calendar
import logging
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.functional import cached_property

from apps.kern.kalender import ganzer_monat, halbmonat, naechster_stichtag, stichtag, tage_zaehlen
from apps.kern.models import Mandant, Zeitstempel
from apps.stammdaten.models import Ansprechpartner, Artikel, Artikeltyp, Kunde, Zahlungsbedingung

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")


def runden(wert: Decimal) -> Decimal:
    return wert.quantize(CENT, rounding=ROUND_HALF_UP)


class Belegart(models.TextChoices):
    ANGEBOT = "AN", "Angebot"
    AUFTRAG = "AB", "Bestellung"
    LIEFERSCHEIN = "LS", "Lieferschein"
    RECHNUNG = "RE", "Rechnung"
    NACHLIEFERSCHEIN = "NL", "Nachlieferschein"
    PROFORMA = "PF", "Proforma-Rechnung"
    GUTSCHRIFT = "GS", "Gutschrift"
    PROFORMA_STORNO = "SP", "Storno Proforma"


class Mailart(models.TextChoices):
    """Belegarten behalten ihr Kürzel, damit die Vorlage direkt über beleg.art gefunden wird."""

    ANGEBOT = "AN", "Angebot"
    AUFTRAG = "AB", "Bestellung"
    LIEFERSCHEIN = "LS", "Lieferschein"
    RECHNUNG = "RE", "Rechnung"
    NACHLIEFERSCHEIN = "NL", "Nachlieferschein"
    PROFORMA = "PF", "Proforma-Rechnung"
    GUTSCHRIFT = "GS", "Gutschrift"
    PROFORMA_STORNO = "SP", "Storno Proforma"
    LIEFERANT = "BE", "Bestellung an Lieferanten"
    ESD = "ES", "Lizenzschlüssel"
    VERSAND = "VS", "Versandmitteilung"
    MAHNUNG = "MA", "Mahnung"
    REPARATUR_ANGENOMMEN = "RA", "Reparatur angenommen"
    REPARATUR_LAEUFT = "RR", "Gerät in Reparatur"
    REPARATUR_FERTIG = "RF", "Reparatur fertig"


# Nächste Stufe im Belegfluss ("per Klick")
FOLGEBELEG = {
    Belegart.ANGEBOT: Belegart.AUFTRAG,
    Belegart.AUFTRAG: Belegart.LIEFERSCHEIN,
    Belegart.LIEFERSCHEIN: Belegart.RECHNUNG,
}
# Beim Übernehmen wird der Beleg selbst umgewandelt (statt kopiert): Angebot -> Bestellung, Bestellung -> Rechnung direkt
UMWANDELBAR = {(Belegart.ANGEBOT, Belegart.AUFTRAG), (Belegart.AUFTRAG, Belegart.RECHNUNG)}
# Preislistenartikel (P) gibt es nur im Angebot; in diese Belegarten werden sie nicht kopiert (Proforma wird später zur Rechnung).
P_UNZULAESSIG = (Belegart.AUFTRAG, Belegart.LIEFERSCHEIN, Belegart.RECHNUNG, Belegart.PROFORMA)
# Diese Belegarten werden erst beim Abschluss nummeriert und danach gesperrt.
GESPERRT_NACH_ABSCHLUSS = {Belegart.RECHNUNG, Belegart.GUTSCHRIFT, Belegart.PROFORMA_STORNO}
# Vorläufige Belege (in der Maske neu angelegt oder umgewandelt, noch nicht abgeschlossen) werden nach so vielen Stunden ohne Aktivität verworfen.
VORLAEUFIG_STUNDEN = 2


class Ruecknahme(models.TextChoices):
    """Spalte B der Gutschrift: Wohin geht die zurückgegebene Ware? Leer = ältere Gutschrift, von Hand zurückzubuchen."""
    REPARATUR = "R", "Reparaturbestand"
    LAGER = "B", "Lagerbestand"
    KEINE = "K", "Keine Ware zurück"


class BelegGesperrt(Exception):
    pass


class Nummernkreis(models.Model):
    """Zähler je Mandant und Art; er läuft über den Jahreswechsel weiter, das Jahr steht nur in der Nummer."""

    # Reihenfolge der Verwaltungsseite; Einkauf und Werkstatt zählen über denselben Kreis, ihre Belege liegen in eigenen Tabellen
    ARTEN = {"RE": "Rechnungen", "GS": "Gutschriften", "LS": "Lieferscheine", "AN": "Angebote", "PF": "Proforma-Rechnungen", "SP": "Stornos zu Proforma-Rechnungen",
             "WS": "Werkstattaufträge", "AB": "Bestellungen", "NL": "Nachlieferscheine", "BE": "Lieferantenbestellungen",
             "WE": "Wareneingänge", "RM": "RMA"}
    QUELLEN = {"WS": "werkstatt.Reparatur", "BE": "einkauf.Bestellung", "WE": "einkauf.Wareneingang", "RM": "einkauf.Rma"}
    NUR_VOR_DEM_START = {"RE", "GS"}  # steuerlich lückenlos: einstellbar nur, solange im Jahr noch keine Nummer vergeben ist
    HOECHSTWERT = 999_999_999

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT)
    art = models.CharField("Belegart", max_length=2, choices=Belegart.choices)
    letzte_nummer = models.PositiveIntegerField("zuletzt vergeben", default=0)
    letzte_vergabe = models.DateField("zuletzt vergeben am", null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Laufende Nummer"
        verbose_name_plural = "Laufende Nummern"
        constraints = [models.UniqueConstraint(fields=["mandant", "art"], name="nummernkreis_je_art")]

    @classmethod
    def vergebene(cls, mandant: Mandant, art: str) -> list:
        """Alle vergebenen Nummern der Art, aus den Belegen selbst und nicht aus dem Zähler."""
        modell = apps.get_model(cls.QUELLEN.get(art, "verkauf.Beleg"))
        filter_ = {"mandant": mandant, "nummer__startswith": art, **({} if art in cls.QUELLEN else {"art": art})}
        nummern = list(modell.objects.filter(**filter_).values_list("nummer", flat=True))
        if art not in cls.QUELLEN:  # umgewandelte Belege (Angebot -> Bestellung -> Rechnung): die alten Nummern bleiben vergeben
            nummern += list(BelegUmwandlung.objects.filter(beleg__mandant=mandant, von_art=art).exclude(von_nummer="")
                            .values_list("von_nummer", flat=True))
        return nummern

    @classmethod
    def gesperrt(cls, mandant: Mandant, art: str, jahr=None) -> bool:
        """Maßgeblich ist das Jahr der Vergabe: Eine im Januar abgeschlossene Dezemberrechnung trägt noch das alte Jahr in der Nummer."""
        if art not in cls.NUR_VOR_DEM_START:
            return False
        jahr = jahr or timezone.localdate().year
        return (cls.objects.filter(mandant=mandant, art=art, letzte_vergabe__year=jahr).exists()
                or any(n.startswith(f"{art}{jahr}-") for n in cls.vergebene(mandant, art)))

    @classmethod
    def naechste_setzen(cls, mandant: Mandant, art: str, naechste: int):
        """Stellt die nächste Nummer ein. Bereits vergebene Nummern kommen nie wieder, auch wenn der Zähler vorher zu hoch stand."""
        if not 1 <= naechste <= cls.HOECHSTWERT:
            raise ValueError(f"Die Nummer muss zwischen 1 und {cls.HOECHSTWERT} liegen.")
        with transaction.atomic():
            kreis, _ = cls.objects.select_for_update().get_or_create(mandant=mandant, art=art)
            if kreis.letzte_nummer + 1 == naechste:
                return
            if cls.gesperrt(mandant, art):
                raise ValueError(f"Im Jahr {timezone.localdate().year} sind schon Nummern vergeben; sie müssen lückenlos weiterlaufen.")
            zaehler = [int(n.rsplit("-", 1)[-1]) for n in cls.vergebene(mandant, art) if n.rsplit("-", 1)[-1].isdigit()]
            if naechste <= max(zaehler, default=0):
                raise ValueError(f"Die Nummer muss größer sein als die höchste vergebene ({max(zaehler)}).")
            kreis.letzte_nummer = naechste - 1
            kreis.save(update_fields=["letzte_nummer"])

    @classmethod
    def naechste(cls, mandant: Mandant, art: str, jahr: int) -> str:
        with transaction.atomic():
            kreis, _ = cls.objects.select_for_update().get_or_create(mandant=mandant, art=art)
            kreis.letzte_nummer += 1
            kreis.letzte_vergabe = timezone.localdate()
            kreis.save(update_fields=["letzte_nummer", "letzte_vergabe"])
        return f"{art}{jahr}-{kreis.letzte_nummer:05d}"

    @classmethod
    def freigeben(cls, mandant: Mandant, art: str, nummer: str):
        """Gibt die zuletzt vergebene Nummer wieder frei (nur wenn sie noch die letzte ist), damit keine Lücke entsteht."""
        try:
            zaehler = int(nummer[len(art):].split("-")[1])
        except (ValueError, IndexError):
            return
        if art not in cls.QUELLEN and BelegUmwandlung.objects.filter(beleg__mandant=mandant, von_art=art, von_nummer=nummer).exists():
            return  # frühere Nummer eines umgewandelten Belegs: bleibt vergeben
        with transaction.atomic():
            kreis = cls.objects.select_for_update().filter(mandant=mandant, art=art).first()
            # aus der Zeit der Jahreszähler kann derselbe Zählerstand in einem anderen Jahr noch vergeben sein
            andere = [n for n in cls.vergebene(mandant, art) if n != nummer and n.rsplit("-", 1)[-1].lstrip("0") == str(zaehler)]
            if kreis and kreis.letzte_nummer == zaehler and not andere:
                kreis.letzte_nummer -= 1
                kreis.save(update_fields=["letzte_nummer"])

    def __str__(self):
        return f"{self.mandant} {self.art}: {self.letzte_nummer}"


class Versandart(Zeitstempel):
    """Stammtabelle: Standardkosten je Versandart; sie werden beim Wählen am Beleg vorbelegt."""

    name = models.CharField("Versandart", max_length=100, unique=True)
    dienstleister = models.CharField("Dienstleister", max_length=60, blank=True)
    versandkosten = models.DecimalField("Versandkosten netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    verpackung = models.DecimalField("Verpackung netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    versicherung = models.DecimalField("Versicherung netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    frachtfrei_ab = models.DecimalField("frachtfrei ab (Netto-Warenwert)", max_digits=12, decimal_places=2, null=True, blank=True,
                                        help_text="Ab diesem Warenwert entfallen die Versandkosten.")
    nur_inland = models.BooleanField("nur Inland", default=False)
    aktiv = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Versandart"
        verbose_name_plural = "Versandarten"
        ordering = ["name"]

    def __str__(self):
        return self.name


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
    rabatt_prozent = models.DecimalField("Belegrabatt %", max_digits=5, decimal_places=2, default=Decimal("0"),
                                         help_text="Wirkt auf alle Positionen (netto, vor der Steuer); Versand, Verpackung und Versicherung bleiben unberührt.")
    bezugnahme = models.CharField("Bezugnahme", max_length=60, blank=True,
                                  help_text="Wie kam die Bestellung zustande? z. B. telefonisch, schriftlich, E-Mail, eBay, Shop.")
    bearbeiter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name="Bearbeiter")
    gueltig_bis = models.DateField("Angebot gültig bis", null=True, blank=True)
    liefertermin = models.DateField("Liefertermin", null=True, blank=True)
    vortext = models.TextField("Text vor den Positionen", blank=True)
    schlusstext = models.TextField("Text nach den Positionen", blank=True)
    bemerkung = models.TextField("Bemerkung (intern, wird nicht gedruckt)", blank=True)
    vorgaenger = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="folgebelege")
    lieferscheine = models.ManyToManyField("self", symmetrical=False, blank=True, related_name="rechnungen",
                                           help_text="Bei Sammelrechnungen: alle Lieferscheine, die abgerechnet werden")
    abgeschlossen = models.BooleanField("abgeschlossen", default=False, editable=False)
    automatisch_erledigt = models.BooleanField(
        "automatisch erledigt", default=False, editable=False,
        help_text="Bestellung: vom Programm abgeschlossen, weil nichts mehr offen war (nicht beim Kunden versandt). "
                  "Nur eine solche Bestellung wird beim Rückbuchen wieder geöffnet.")
    nl_lager_abbuchen = models.BooleanField(
        "Nachlieferschein: Ware vom Lager abbuchen", null=True, blank=True, editable=False,
        help_text="Leer = noch nicht beantwortet. Ja bei Ersatzlieferung (z. B. defektes Gerät), nein bei bloßer Nachlieferung.")
    vorlaeufig = models.BooleanField(
        "vorläufig (nicht gespeichert)", default=False, db_index=True, editable=False,
        help_text="In der Belegmaske neu angelegt oder umgewandelt und noch nicht mit „Speichern: abschließen & Nummer vergeben“ "
                  "gespeichert: ohne Nummer; wird verworfen (alles zurückgenommen), wenn er nicht abgeschlossen wird.")
    zuletzt_aktiv = models.DateTimeField("zuletzt aktiv", null=True, blank=True, editable=False,
                                         help_text="Vorläufiger Beleg: letzte Aktivität des Benutzers; nach 2 Stunden ohne Aktivität wird verworfen.")
    abo = models.ForeignKey("Abo", on_delete=models.SET_NULL, null=True, blank=True, related_name="belege", editable=False,
                            help_text="Wiederkehrende Rechnung, aus der die Bestellung entstand")
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
        return f"{self.get_art_display()} {self.nummer or ('(nicht gespeichert)' if self.vorlaeufig else '(Entwurf)')}"

    def vorlaeufig_sperre(self, aktion):
        """Vor dem Speichern (Abschluss mit Nummer) gibt es gar nichts nach außen: kein PDF, Druck, keine Mail, Zahlung, kein Versand."""
        if self.vorlaeufig:
            raise ValueError(f"{aktion} erst nach „Speichern: abschließen & Nummer vergeben“.")

    @property
    def vorgang_oder_none(self):
        if not self.pk or not self.vorlaeufig:
            return None
        return BelegVorgang.objects.filter(beleg_id=self.pk).first()

    @property
    def gehoert_zu_vorgang(self):
        """Das nicht gespeicherte P-Angebot eines anderen vorläufigen Belegs: der Beleg, zu dem es gehört (sonst None). Es hat
        keinen eigenen Vorgang; Nummer, Speichern und Verwerfen folgen dem Beleg, in dessen Übernahme es entstanden ist."""
        if not self.pk or not self.vorlaeufig:
            return None
        v = BelegVorgang.objects.filter(angebot_id=self.pk).select_related("beleg").first()
        return v.beleg if v is not None else None

    @property
    def vorlaeufiges_angebot(self) -> bool:
        """Nicht gespeichertes Angebot mit eigenem Vorgang (neu angelegt): Übernehmen und Kopieren daraus fragen nicht nach (Regel
        des Nutzers); das Angebot wird erst mit dem Abschluss des neuen Belegs abgeschlossen und nummeriert, beim Verwerfen
        des neuen Belegs ist alles wieder wie vorher (vorgang.angebot_vorab, beleg_verwerfen)."""
        return bool(self.pk and self.art == Belegart.ANGEBOT and self.vorlaeufig
                    and BelegVorgang.objects.filter(beleg_id=self.pk, weg=Vorgangsweg.NEU).exists())

    @property
    def offenes_ziel(self):
        """Nicht gespeicherter Beleg, der aus diesem vorläufigen Angebot entstanden ist und es beim Speichern mit abschließt
        (sonst None). Solange er besteht, lässt sich das Angebot nicht verwerfen."""
        if not self.pk or not self.vorlaeufig or self.art != Belegart.ANGEBOT:
            return None
        v = BelegVorgang.objects.filter(quelle_id=self.pk, quelle_abgeschlossen_durch_vorgang=True).exclude(beleg_id=self.pk) \
            .select_related("beleg").first()
        return v.beleg if v is not None else None

    @property
    def verwerfen_moeglich(self) -> bool:
        """Vorläufiger Beleg mit Vorgang: „Verwerfen“ nimmt alles zurück (für das Template)."""
        return bool(self.pk and self.vorlaeufig and not self.abgeschlossen and BelegVorgang.objects.filter(beleg_id=self.pk).exists())

    @property
    def gesperrt(self) -> bool:
        return self.abgeschlossen and (self.art in GESPERRT_NACH_ABSCHLUSS or self.proforma_fest)

    @property
    def proforma_fest(self) -> bool:
        """Abgeschlossene Proforma mit Zahlung, Rechnung oder Storno: Positionen und Beträge sind fest (Nachvollziehbarkeit)."""
        if self.art != Belegart.PROFORMA or not self.abgeschlossen or not self.pk:
            return False
        return self.zahlungen.exists() or self.folgebelege.filter(art__in=(Belegart.RECHNUNG, Belegart.PROFORMA_STORNO)).exists()

    @property
    def proforma_storno(self):
        """Storno-Beleg zu dieser Proforma (None = nicht storniert)."""
        if self.art != Belegart.PROFORMA or not self.pk:
            return None
        return self.folgebelege.filter(art=Belegart.PROFORMA_STORNO).first()

    @property
    def proforma_rechnung(self):
        """Rechnung, in die diese Proforma umgewandelt wurde (None = noch nicht umgewandelt)."""
        if self.art != Belegart.PROFORMA or not self.pk:
            return None
        return self.folgebelege.filter(art=Belegart.RECHNUNG).first()

    @property
    def positionen_gesperrt(self) -> bool:
        """Positionen sind nach Abschluss fest: bei Rechnung/Gutschrift (GoBD), bei Lieferschein/Nachlieferschein (Lagerbuchung)
        und bei einer Proforma mit Zahlung, Rechnung oder Storno."""
        return self.abgeschlossen and (self.gesperrt or self.art in (Belegart.LIEFERSCHEIN, Belegart.NACHLIEFERSCHEIN))

    @cached_property
    def hat_versand(self) -> bool:
        """Gibt es zum Beleg eine Sendung (Label/Etikett)? Listen setzen den Wert vorab für alle Zeilen (versand_markieren)."""
        return self.versendungen.exists()

    @cached_property
    def lieferscheine_aus_bestellung(self) -> list:
        """Bestellung: daraus erstellte Lieferscheine (Markierung „L“). Listen setzen den Wert vorab (versand_markieren)."""
        if self.art != Belegart.AUFTRAG:
            return []
        return list(self.folgebelege.filter(art=Belegart.LIEFERSCHEIN).order_by("pk").only("pk", "nummer", "art", "vorgaenger_id"))

    @cached_property
    def rechnungen_aus_bestellung(self) -> list:
        """Bestellung: direkt daraus erstellte Rechnungen (Markierung „R“); eine Rechnung aus dem Lieferschein zählt hier
        nicht, die hängt am Lieferschein. Listen setzen den Wert vorab (versand_markieren)."""
        if self.art != Belegart.AUFTRAG:
            return []
        return list(self.folgebelege.filter(art=Belegart.RECHNUNG).order_by("pk").only("pk", "nummer", "art", "vorgaenger_id"))

    @staticmethod
    def versand_markieren(belege):
        """Setzt hat_versand und (bei Bestellungen) die Lieferscheine und Rechnungen für die Markierungen „L“ und „R“
        für eine ganze Liste mit je einer Abfrage (statt einer je Zeile)."""
        belege = list(belege)
        mit = set(Versand.objects.filter(beleg__in=belege).values_list("beleg_id", flat=True))
        folge = {Belegart.LIEFERSCHEIN: {}, Belegart.RECHNUNG: {}}
        auftraege = [b.pk for b in belege if b.art == Belegart.AUFTRAG]
        if auftraege:
            for f in Beleg.objects.filter(vorgaenger_id__in=auftraege, art__in=list(folge)).order_by("pk").only(
                    "pk", "nummer", "art", "vorgaenger_id"):
                folge[f.art].setdefault(f.vorgaenger_id, []).append(f)
        for b in belege:
            b.hat_versand = b.pk in mit
            ist_ab = b.art == Belegart.AUFTRAG
            b.lieferscheine_aus_bestellung = folge[Belegart.LIEFERSCHEIN].get(b.pk, []) if ist_ab else []
            b.rechnungen_aus_bestellung = folge[Belegart.RECHNUNG].get(b.pk, []) if ist_ab else []
        return belege

    def loeschhindernis(self):
        """Grund, warum der Beleg nicht gelöscht werden darf (None = löschbar).
        Löschbar sind Angebote, Bestellungen (auch nach dem Übernehmen in einen Folgebeleg) und noch nicht
        nummerierte Rechnungs-/Gutschriftentwürfe."""
        zu_vorgang = self.gehoert_zu_vorgang
        if zu_vorgang is not None:
            return (f"Das Angebot gehört zum nicht gespeicherten Beleg {zu_vorgang} und wird mit ihm gespeichert oder verworfen – "
                    "bitte dort „Verwerfen“ wählen.")
        if self.art not in (Belegart.ANGEBOT, Belegart.AUFTRAG, Belegart.PROFORMA) and not (
                self.art in GESPERRT_NACH_ABSCHLUSS and not self.abgeschlossen and not self.nummer):
            return f"{self.get_art_display()} kann nicht gelöscht werden – Korrektur nur per Storno/Gutschrift."
        if self.pk and self.art in GESPERRT_NACH_ABSCHLUSS:  # die frühere Bestellung samt Nummer wäre sonst mit weg
            letzte = self.umwandlungen.order_by("-pk").first()
            if letzte:
                return (f"Die {self.get_art_display()} ist aus {Belegart(letzte.von_art).label} {letzte.von_nummer} umgewandelt "
                        "und kann nicht gelöscht werden – die frühere Belegnummer muss erhalten bleiben.")
        if self.art == Belegart.PROFORMA and self.folgebelege.filter(art__in=(Belegart.RECHNUNG, Belegart.PROFORMA_STORNO)).exists():
            return "Aus der Proforma-Rechnung ist schon eine Rechnung oder ein Storno entstanden – sie muss erhalten bleiben."
        if self.art in (Belegart.PROFORMA, Belegart.AUFTRAG) and self.anzahlung_verrechnungen.exists():
            return "Zahlungen dieses Belegs sind mit einer Rechnung verrechnet – er muss erhalten bleiben."
        if self.rechnungen.exists():  # nur bei Lieferscheinen möglich; Angebote/Bestellungen dürfen nach dem Übernehmen gelöscht werden
            return "Der Beleg wird in einer Rechnung abgerechnet – bitte zuerst diese entfernen."
        if self.mails.exists():
            return "Der Beleg wurde per E-Mail versendet; der Versandnachweis muss erhalten bleiben."
        if self.zahlungen.exists():
            return "Zum Beleg gibt es Zahlungen (Anzahlung) – bitte zuerst stornieren."
        if self.lagerbewegungen.exists():
            return "Zum Beleg gibt es Lagerbuchungen."
        if self.versendungen.filter(dienst="dp").exists():
            return "Zum Beleg wurde eine Briefmarke gekauft – der Nachweis muss erhalten bleiben."
        return None

    def p_positionen(self):
        """Positionen mit Preislistenartikel (Typ P) ohne eigenen Artikel: in Bestellung, Lieferschein und Rechnung gelten sie nur als Angebot."""
        return self.positionen.filter(preisliste_artikel__isnull=False, artikel__isnull=True)

    @property
    def p_hinweis(self) -> str:
        """Text, solange Bestellung/Lieferschein/Rechnung noch Preislistenartikel (P) enthält; leer = in Ordnung."""
        if self.art not in (Belegart.AUFTRAG, Belegart.LIEFERSCHEIN, Belegart.RECHNUNG) or self.gesperrt:
            return ""
        if self.art != Belegart.AUFTRAG and self.abgeschlossen:
            return ""
        if self.p_positionen().exists():
            return (f"{self.get_art_display()} enthält Preislistenartikel (P): diese Positionen gelten nur als Angebot. "
                    "Drucken, Abschließen und Versenden geht erst, wenn alle Positionen eigene Artikel (A) sind – "
                    "nach dem Wareneingang werden sie automatisch umgestellt.")
        return ""

    def p_hauptpositionen(self) -> set:
        """IDs der Hauptpositionen, die als P zählen: P-Positionen selbst und Gruppen mit mindestens einer P-Unterposition."""
        return {gruppe or pk for pk, gruppe in self.p_positionen().values_list("pk", "gruppe_id")}

    def echte_hauptpositionen(self) -> set:
        """IDs der Hauptpositionen, die etwas Übernehmbares sind (mit Artikel oder Gruppenkopf); freie Text-/Leerpositionen zählen nicht."""
        return set(self.positionen.filter(gruppe__isnull=True).filter(
            models.Q(artikel__isnull=False) | models.Q(typ="G")).values_list("pk", flat=True))

    def _p_vorbereiten(self, art):
        """Preislistenartikel (P) sind in Bestellung, Lieferschein, Rechnung und Proforma nicht erlaubt.
        Rückgabe: (IDs der P-Hauptpositionen, die nicht mitkopiert werden, bei Bestellung → LS/RE/PF aber ins Angebot wandern: ja/nein)."""
        if art not in P_UNZULAESSIG:
            return set(), False
        ids = self.p_hauptpositionen()
        if not ids:
            return set(), False
        verschieben = self.art == Belegart.AUFTRAG and art in (Belegart.LIEFERSCHEIN, Belegart.RECHNUNG, Belegart.PROFORMA)
        if not verschieben and not (self.echte_hauptpositionen() - ids):
            raise ValueError(f"{self.get_art_display()} enthält nur Preislistenartikel (P) – sie bleiben {self.get_art_display()}, "
                             "bis sie als eigene Artikel (A) angelegt sind.")
        return ids, verschieben

    def _p_ins_angebot_verschieben(self, ids, ziel=None) -> "Beleg":
        """Bestellung: P-Positionen (Gruppen samt Unterpositionen) wandern in ein neues Angebot desselben Kunden.
        Ist das Ziel (Lieferschein, Rechnung, Proforma) vorläufig, hat das Angebot noch keine Nummer: es hängt am Vorgang des
        Ziels, bekommt die Nummer beim Abschluss und verschwindet beim Verwerfen (die Positionen gehen zurück)."""
        vorlaeufig = ziel is not None and ziel.vorlaeufig
        angebot = Beleg.objects.create(
            vorlaeufig=vorlaeufig,
            mandant=self.mandant, art=Belegart.ANGEBOT, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=self.zahlungsbedingung, rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=self.versandkosten, verpackung=self.verpackung, versicherung=self.versicherung,
            rabatt_prozent=self.rabatt_prozent, bezugnahme=self.bezugnahme, bearbeiter=self.bearbeiter,
            referenz=self.referenz, vortext=self.vortext, schlusstext=self.schlusstext,
            bemerkung=self.bemerkung, vorgaenger=self)
        if vorlaeufig:
            BelegVorgang.objects.filter(beleg=ziel).update(angebot=angebot)
        for p in Position.objects.filter(models.Q(pk__in=ids) | models.Q(gruppe_id__in=ids)):
            p.beleg = angebot
            p.save()  # nicht update(): das Änderungsprotokoll hält das Umhängen fest
        self.berechne()
        angebot.berechne()
        return angebot

    def _p_abschluss(self, neu, anzahl, ids, verschieben, angebot):
        """Meldungsinfo am neuen Beleg; eine umgewandelte Bestellung ohne offene Reste wird abgeschlossen (erledigt)."""
        if not ids:
            return
        neu.p_info = {"anzahl": anzahl, "angebot": angebot, "quelle": self}
        if verschieben:
            self.berechne()
            # Proforma kann storniert werden: die Bestellung wird dann wieder geöffnet (Rückbuchen)
            if neu.art != Belegart.PROFORMA and not self.offene_mengen() and not self.abgeschlossen:
                from . import vorgang
                if neu.vorlaeufig and vorgang.abschluss_vormerken(neu, "erledigt"):
                    pass  # die Bestellung bleibt bis zum Abschluss des vorläufigen Ziels offen; beim Verwerfen ist nichts zurückzudrehen
                elif self.positionen.exists():
                    self.abschliessen()
                    self.automatisch_erledigt = True
                    Beleg.objects.filter(pk=self.pk).update(automatisch_erledigt=True)
                else:  # leer gewordene Bestellung: abschliessen() fehlt bei Belegen ohne Position
                    from .uebernahme import erledigt_wenn_leer
                    erledigt_wenn_leer(self)
                neu.p_info["abgeschlossen"] = True
            elif neu.art != Belegart.PROFORMA and self.abgeschlossen:
                neu.p_info["abgeschlossen"] = True

    def positionen_pruefen(self, art=None):
        """Ein Beleg ohne Position darf nicht abgeschlossen, versendet oder weitergeführt werden. Eine Bestellung, deren
        ganze Menge schon in Folgebelege wanderte, ist nicht „leer“, sondern erledigt: Beim Weiterführen (art) gilt dann die
        Prüfung auf Proforma und offene Mengen."""
        if not self.positionen.exists():
            if art and self.art == Belegart.AUFTRAG and self.uebernahmen_aus.exists():
                self.folge_pruefen(art)  # nennt die aktive Proforma bzw. „nichts mehr offen“
            raise ValueError(f"{self} hat keine Position – erst Positionen erfassen.")

    @property
    def lieferdatum(self):
        """Lieferdatum im Kopf einer Rechnung: ohne Lieferschein = Rechnungsdatum; stammen alle Positionen aus
        einem Lieferschein, dessen Datum. Bei mehreren Lieferscheinen None (Datum steht je Position)."""
        if self.art != Belegart.RECHNUNG:
            return None
        if not self.pk:
            return self.belegdatum
        haupt = self.positionen.filter(gruppe__isnull=True)
        ls_ids = set(haupt.values_list("lieferschein_id", flat=True))
        if ls_ids == {None} or not ls_ids:
            return self.belegdatum
        if len(ls_ids) == 1:
            return Beleg.objects.filter(pk=ls_ids.pop()).values_list("belegdatum", flat=True).first()
        return None

    @property
    def abrechenbar(self) -> bool:
        """Abgeschlossener Lieferschein, der noch in keine Rechnung (und keinen anderen Folgebeleg) übernommen wurde."""
        return (self.art == Belegart.LIEFERSCHEIN and self.abgeschlossen
                and not self.folgebelege.filter(art=Belegart.RECHNUNG).exists() and not self.rechnungen.exists())

    @property
    def folgeart(self):
        return FOLGEBELEG.get(self.art)

    @property
    def folgeart_name(self):
        return Belegart(self.folgeart).label if self.folgeart else ""

    @cached_property
    def verlauf(self) -> list:
        """Alle Umwandlungen des Belegs (älteste zuerst), z. B. Angebot -> Bestellung -> Rechnung."""
        return sorted(self.umwandlungen.all(), key=lambda u: u.pk) if self.pk else []  # Listen holen sie vorab (prefetch_related)

    @property
    def vormals(self) -> str:
        """Hinweis „Angebot AN2026-00001, Bestellung AB2026-00003“ aus dem Verlauf; leer, wenn der Beleg nie umgewandelt wurde."""
        return ", ".join(f"{u.von_art_name} {u.von_nummer}".strip() for u in self.verlauf)

    @property
    def bezug_text(self) -> str:
        """Bezug im Kopf des Ausdrucks: die letzte frühere Nummer aus dem Verlauf (der direkte Vorgänger-Beleg, z. B. die
        Bestellung), sonst der Vorgänger mit Nummer; sonst leer."""
        for u in reversed(self.verlauf):
            if u.von_nummer:
                return f"{u.von_art_name} {u.von_nummer}"
        if self.vorgaenger_id and self.vorgaenger.nummer:
            return f"{self.vorgaenger.get_art_display()} {self.vorgaenger.nummer}"
        return ""

    def save(self, *args, **kwargs):
        if self.pk and self.gesperrt:
            vorher = Beleg.objects.get(pk=self.pk)
            # nur Versandart und Zahlungsbedingung bleiben änderbar
            for feld in ("kunde_id", "belegdatum", "versandkosten", "verpackung", "versicherung", "summe_netto", "summe_brutto", "art", "nummer"):
                if getattr(vorher, feld) != getattr(self, feld):
                    raise BelegGesperrt(f"{self}: Feld '{feld}' ist nach Abschluss gesperrt.")
        self.nummer = self.nummer or None
        if not self.pk and not self.nummer and self.art not in GESPERRT_NACH_ABSCHLUSS and not self.vorlaeufig:
            self.nummer = Nummernkreis.naechste(self.mandant, self.art, self.belegdatum.year)
        if not self.pk and self.vorlaeufig and self.zuletzt_aktiv is None:
            self.zuletzt_aktiv = timezone.now()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Ein Folgebeleg aus einer Bestellung (Rechnungsentwurf, Proforma) gibt die übernommene Menge an die Bestellung zurück."""
        with transaction.atomic():
            if self.pk and (not self.abgeschlossen or self.art == Belegart.PROFORMA):  # Gelieferte/abgerechnete Belege geben nichts zurück
                from .uebernahme import beleg_zurueckbuchen
                beleg_zurueckbuchen(self, ziel_anpassen=False)  # die Positionen werden ohnehin mitgelöscht
            return super().delete(*args, **kwargs)

    @classmethod
    def leere_entwuerfe_entfernen(cls, mandant, kunde=None, minuten=2):
        """Belege ohne Position werden nicht aufbewahrt: leere, nicht abgeschlossene Entwürfe, die älter als ein paar Minuten
        sind und an denen nichts hängt, werden gelöscht; ihre Nummer wird wieder frei, wenn sie die letzte vergebene war."""
        BelegArbeitsstand.objects.filter(beleg__mandant=mandant).delete()  # Altdaten: der aktuelle Stand gilt
        grenze = timezone.now() - timedelta(minutes=minuten)
        kandidaten = cls.objects.filter(mandant=mandant, abgeschlossen=False, angelegt_am__lt=grenze, positionen__isnull=True,
                                        arbeitsstand__isnull=True, vorlaeufig=False)  # vorläufige Belege laufen über vorlaeufige_verwerfen
        if kunde is not None:
            kandidaten = kandidaten.filter(kunde=kunde)
        anzahl = 0
        for b in kandidaten.order_by("-id"):
            if b.folgebelege.exists() or b.rechnungen.exists() or b.mails.exists() or b.zahlungen.exists() \
                    or b.lagerbewegungen.exists() or b.versendungen.exists() or b.umwandlungen.exists():
                continue
            nummer, art = b.nummer, b.art
            b.delete()
            if nummer:
                Nummernkreis.freigeben(mandant, art, nummer)
            anzahl += 1
        return anzahl

    @classmethod
    def vorlaeufige_verwerfen(cls, mandant=None, ausfuehren=True, jetzt=None) -> list:
        """Vorläufige Belege (nicht gespeichert), an denen seit VORLAEUFIG_STUNDEN Stunden nichts mehr getan wurde, werden verworfen:
        alles wird zurückgenommen (siehe vorgang.beleg_verwerfen). Je Beleg eine eigene Transaktion; der Zeitpunkt wird unter der
        Sperre erneut geprüft, ein Puls dazwischen verhindert das Verwerfen. Shop-, Abo- und Werkstattbelege und Altentwürfe
        (nicht vorläufig) werden nie angefasst. ausfuehren=False: nur auflisten.
        Rückgabe: Liste von {"beleg", "weg", "verworfen", "fehler"}."""
        from . import vorgang
        jetzt = jetzt or timezone.now()
        grenze = jetzt - timedelta(hours=VORLAEUFIG_STUNDEN)
        zu_alt = models.Q(zuletzt_aktiv__lt=grenze) | models.Q(zuletzt_aktiv__isnull=True, angelegt_am__lt=grenze)
        # Abo-Bezug schützt nur Belege, die selbst dazugehören; ein vorläufiger Folgebeleg oder eine Umwandlung aus einer
        # Abo-Bestellung wird verworfen (die Bestellung steht danach wieder wie vorher da).
        kandidaten = cls.objects.filter(zu_alt, vorlaeufig=True, abgeschlossen=False, vorgang__isnull=False).exclude(
            abo__isnull=False, vorgang__weg__in=(Vorgangsweg.NEU, Vorgangsweg.GUTSCHRIFT))
        # Ein nicht gespeichertes Angebot, aus dem ein ebenfalls nicht gespeicherter Beleg entstanden ist, bleibt bestehen; wird der
        # Beleg verworfen, beginnt die Frist des Angebots neu.
        kandidaten = kandidaten.exclude(pk__in=BelegVorgang.objects.filter(quelle__isnull=False, quelle_abgeschlossen_durch_vorgang=True)
                                        .values("quelle_id"))
        if mandant is not None:
            kandidaten = kandidaten.filter(mandant=mandant)
        ergebnis = []
        for b in kandidaten.select_related("kunde", "vorgang").order_by("pk"):
            eintrag = {"beleg": b, "weg": b.vorgang.get_weg_display(), "verworfen": False, "fehler": ""}
            ergebnis.append(eintrag)
            if not ausfuehren:
                continue
            try:
                with transaction.atomic():
                    quelle_id = BelegVorgang.objects.filter(beleg_id=b.pk).values_list("quelle_id", flat=True).first()
                    if quelle_id:  # überall dieselbe Reihenfolge wie beim Verwerfen von Hand: erst die Quelle, dann das Ziel (kein Deadlock)
                        cls.objects.select_for_update().filter(pk=quelle_id).first()
                    aktuell = cls.objects.select_for_update().filter(pk=b.pk).filter(zu_alt, vorlaeufig=True, abgeschlossen=False).first()
                    if aktuell is None:  # inzwischen aktiv, abgeschlossen oder schon weg
                        continue
                    eintrag["verworfen"] = vorgang.beleg_verwerfen(aktuell, None, "automatisch") is not None
            except (ValueError, models.ProtectedError) as fehler:
                eintrag["fehler"] = str(fehler)
                logger.warning("Vorläufiger Beleg %s lässt sich nicht verwerfen: %s", b.pk, fehler)
            except Exception:
                eintrag["fehler"] = "unerwarteter Fehler (siehe Protokoll)"
                logger.exception("Vorläufiger Beleg %s: Verwerfen fehlgeschlagen", b.pk)
        return ergebnis

    def arbeitsstand_beenden(self) -> bool:
        """Altdaten: Ein früher angelegter Arbeitsstand („Nicht gespeicherte Änderungen“) wird still beendet, der aktuelle Stand gilt.
        Neue Arbeitsstände entstehen nicht mehr. True, wenn es einen gab."""
        return BelegArbeitsstand.objects.filter(beleg=self).delete()[0] > 0

    def stand_pruefen(self):
        """Positionsänderungen gelten sofort; ein Arbeitsstand aus Altdaten wird still beendet."""
        self.arbeitsstand_beenden()

    def ek_und_marge(self):
        """(EK netto, Marge in % vom Netto-VK, unvollständig) für die interne Anzeige; None bei Belegarten ohne Anzeige.
        EK je Position: Lagerartikel/Gruppen über Wareneingang bzw. günstigste Preisliste, Preislistenartikel mit ihrem EK;
        Leistungen mit ihren Kosten; Gruppen zählen über ihre Unterpositionen. Leistungen ohne Kosten und freie Positionen haben keinen EK (unvollständig = True)."""
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
            if p.artikel_id and p.artikel.typ in ("A", "G", "L"):
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
        roh = {}
        for p in self.positionen.all():
            roh[p.steuer_prozent] = roh.get(p.steuer_prozent, Decimal("0")) + p.netto_gesamt
        saetze, _ = self.nach_rabatt(roh)
        netto = sum(saetze.values(), Decimal("0"))
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

    def artikelgewicht(self):
        """Paketgewicht aus den Artikelgewichten (Menge × Gewicht). Rückgabe: (kg, Bezeichnungen ohne Gewicht).
        Leistungen und ESD-Keys wiegen nichts und zählen nicht als fehlend. Bei einer Gruppe (z. B. Komplett-PC)
        zählt das Gewicht des Gruppenartikels, wenn es gepflegt ist – sonst die Summe der Unterpositionen."""
        summe, fehlt = Decimal("0"), []
        positionen = list(self.positionen.select_related("artikel", "preisliste_artikel"))
        mit_kindern = {p.gruppe_id for p in positionen if p.gruppe_id}
        gewogen = set()  # Gruppen, deren Gewicht schon über den Gruppenartikel zählt
        for p in positionen:
            if p.typ == Artikeltyp.G and p.menge > 0:
                if p.artikel and p.artikel.gewicht_kg:
                    summe += p.menge * p.artikel.gewicht_kg
                    gewogen.add(p.pk)
                elif p.pk not in mit_kindern and p.artikel:
                    fehlt.append(p.bezeichnung)
        for p in positionen:
            if p.typ == Artikeltyp.G or p.gruppe_id in gewogen:
                continue
            if p.menge <= 0 or p.typ == Artikeltyp.L or (p.artikel and p.artikel.esd_key):
                continue
            quelle = p.artikel or p.preisliste_artikel
            if quelle is None:
                continue  # freie Textposition
            if quelle.gewicht_kg:
                summe += p.menge * quelle.gewicht_kg
            else:
                fehlt.append(p.bezeichnung)
        return summe.quantize(Decimal("0.01"), rounding=ROUND_UP), fehlt

    def nach_rabatt(self, roh: dict):
        """Belegrabatt je Steuersatz abziehen: (Steuerbasis nach Rabatt, Rabattbetrag je Satz). Rundung je Satz auf Cent."""
        rabatte = {satz: runden(basis * self.rabatt_prozent / 100) for satz, basis in roh.items()} if self.rabatt_prozent else {}
        return {satz: basis - rabatte.get(satz, Decimal("0")) for satz, basis in roh.items()}, rabatte

    def versandart_setzen(self, name: str):
        """Versandart wählen; gehört sie zur Stammtabelle, werden Standardkosten vorbelegt (frachtfrei ab Warenwert, nur Inland)."""
        name = (name or "").strip()
        stamm = Versandart.objects.filter(name__iexact=name, aktiv=True).first() if name else None
        if stamm and stamm.nur_inland and (self.kunde.land or "DE") != "DE":
            raise ValueError(f"Die Versandart „{stamm.name}“ gilt nur im Inland.")
        self.versandart = stamm.name if stamm else name
        if stamm:
            warenwert = sum((p.netto_gesamt for p in self.positionen.all()), Decimal("0"))
            frei = stamm.frachtfrei_ab is not None and warenwert >= stamm.frachtfrei_ab
            self.versandkosten = Decimal("0") if frei else stamm.versandkosten
            self.verpackung, self.versicherung = stamm.verpackung, stamm.versicherung

    @transaction.atomic
    def abschliessen(self, p_erlaubt=False):
        """p_erlaubt: nur für „erst abschließen, dann übernehmen“ (views._erst_abschliessen): eine Bestellung mit Preislistenartikeln
        (P) darf abgeschlossen werden, weil die Übernahme die P-Positionen sofort in ein neues Angebot verschiebt."""
        if self.abgeschlossen:
            return
        self.arbeitsstand_beenden()
        # Sperrreihenfolge wie beim Verwerfen (erst die Quelle, dann das Ziel): sonst Deadlock, wenn gleichzeitig verworfen wird.
        # Nur nötig, wenn der Abschluss der Quelle vorgemerkt ist (nachholen sperrt sie).
        vorab = BelegVorgang.objects.filter(beleg_id=self.pk, quelle_abgeschlossen_durch_vorgang=True).values_list("quelle_id", flat=True).first()
        if vorab:
            Beleg.objects.select_for_update().filter(pk=vorab).first()
        # Zeile sperren und Zustand neu lesen: Doppelklick/zweiter Tab darf keine zweite Nummer vergeben
        schon_abgeschlossen, war_vorlaeufig =Beleg.objects.select_for_update().values_list("abgeschlossen", "vorlaeufig").get(pk=self.pk)
        if schon_abgeschlossen:
            self.refresh_from_db()
            return
        self.vorlaeufig = war_vorlaeufig
        zu_vorgang = self.gehoert_zu_vorgang
        if zu_vorgang is not None:
            raise ValueError(f"Das Angebot gehört zum nicht gespeicherten Beleg {zu_vorgang}: Es bekommt seine Nummer, sobald dieser "
                             "mit „Speichern: abschließen & Nummer vergeben“ gespeichert wird (sonst wird beides verworfen).")
        if not self.positionen.exists():
            raise ValueError("Beleg ohne Positionen kann nicht abgeschlossen werden.")
        if self.art in (Belegart.AUFTRAG, Belegart.LIEFERSCHEIN, Belegart.RECHNUNG) and not (p_erlaubt and self.art == Belegart.AUFTRAG) \
                and self.p_positionen().exists():
            raise ValueError(f"{self.get_art_display()} enthält Preislistenartikel (P) – nur als Angebot möglich. "
                             "Erst Wareneingang buchen, dann sind es eigene Artikel (A).")
        if self.art == Belegart.RECHNUNG:  # Die Ware muss über den Lieferschein abgebucht sein, sonst bliebe der Bestand falsch
            from apps.lager.services import offene_lieferscheine
            offen = offene_lieferscheine(self)
            if offen:
                raise ValueError("Erst den Lieferschein " + ", ".join(l.nummer or "(Entwurf)" for l in offen) +
                                 " abschließen (bucht das Lager ab), dann die Rechnung.")
        self.berechne()
        if self.art == Belegart.GUTSCHRIFT and self.vorgaenger_id and self.vorgaenger.art == Belegart.RECHNUNG:
            andere = sum((g.summe_brutto for g in self.vorgaenger.folgebelege.filter(art=Belegart.GUTSCHRIFT, abgeschlossen=True)
                          .exclude(pk=self.pk)), Decimal("0"))
            if andere + self.summe_brutto > self.vorgaenger.summe_brutto + CENT:
                raise ValueError(f"Die Gutschriften ({andere + self.summe_brutto} €) würden die Rechnung "
                                 f"{self.vorgaenger.nummer} ({self.vorgaenger.summe_brutto} €) übersteigen.")
        if self.art == Belegart.GUTSCHRIFT:
            from apps.lager.services import gutschrift_menge_pruefen
            gutschrift_menge_pruefen(self)
        if war_vorlaeufig and self.art in (Belegart.RECHNUNG, Belegart.GUTSCHRIFT) and self.belegdatum < timezone.localdate():
            self.belegdatum = timezone.localdate()  # Rechnung/Gutschrift tragen das Abschlussdatum, nicht den Tag des Anlegens
        if war_vorlaeufig:  # Angebot, aus dem dieser Beleg übernommen wurde: bekommt zuerst seine Nummer (Regel des Nutzers)
            from . import vorgang
            vorgang.angebot_vorab(self)
        if not self.nummer:
            self.nummer = Nummernkreis.naechste(self.mandant, self.art, self.belegdatum.year)
        self.abgeschlossen, self.vorlaeufig, self.zuletzt_aktiv = True, False, None
        Beleg.objects.filter(pk=self.pk).update(nummer=self.nummer, abgeschlossen=True, vorlaeufig=False, zuletzt_aktiv=None,
                                                belegdatum=self.belegdatum)
        from apps.lager.services import buche_beleg  # Lieferschein/Rechnung buchen den Bestand ab
        from apps.kern.middleware import aktueller_benutzer
        benutzer = aktueller_benutzer()
        benutzer = benutzer if benutzer is not None and benutzer.is_authenticated else None
        buche_beleg(self, benutzer)
        if self.art == Belegart.GUTSCHRIFT:
            from apps.lager.services import gutschrift_buchen
            gutschrift_buchen(self, benutzer)
        from apps.esd.services import beleg_zuordnen  # ESD-Keys beim Verkauf automatisch zuordnen
        beleg_zuordnen(self, benutzer)
        if self.art == Belegart.RECHNUNG:  # Anzahlungen aus Proforma/Bestellung in derselben Transaktion verrechnen
            from apps.finanzen.anzahlung import anzahlungen_verrechnen
            anzahlungen_verrechnen(self, benutzer)
        if war_vorlaeufig:
            self._vorgang_nachholen()

    def _vorgang_nachholen(self):
        """Der Abschluss macht den Vorgang endgültig: P-Angebot nummerieren, vorgemerkten Abschluss der Quelle ausführen
        (Bestellung erledigt, Urbeleg abgeschlossen), Vorgang löschen. Die Sperre liegt hier auf Ziel vor Quelle."""
        from . import vorgang
        vorgang.nachholen(self)

    def folge_pruefen(self, art):
        """Verhindert doppelte Abrechnung und doppelte Lieferung aus demselben Ursprungsbeleg."""
        if self.art == Belegart.LIEFERSCHEIN and art == Belegart.RECHNUNG and not self.abrechenbar:
            raise ValueError(f"{self} ist schon abgerechnet (oder nicht abrechenbar).")
        if self.art == Belegart.AUFTRAG and art in (Belegart.LIEFERSCHEIN, Belegart.RECHNUNG, Belegart.PROFORMA):
            proforma = self.folgebelege.filter(art=Belegart.PROFORMA).exclude(folgebelege__art=Belegart.PROFORMA_STORNO).first()
            if proforma:
                raise ValueError(f"Zu {self} gibt es die {proforma} (Vorkasse): Die Rechnung entsteht dort mit "
                                 "„In Rechnung umwandeln“, sobald sie bezahlt ist.")
            if not self.offene_mengen():
                raise ValueError(f"Zu {self} gibt es schon einen Lieferschein oder eine Rechnung – es ist nichts mehr offen (keine zweite Abrechnung).")
        if self.art == Belegart.PROFORMA and art == Belegart.RECHNUNG:
            raise ValueError("Eine Proforma-Rechnung wird mit „In Rechnung umwandeln“ zur Rechnung, sobald sie vollständig bezahlt ist.")
        if art == Belegart.PROFORMA and self.art != Belegart.AUFTRAG:
            raise ValueError("Eine Proforma-Rechnung entsteht nur aus einer Bestellung.")

    def offene_mengen(self) -> dict:
        """Bestellung: offene Menge je Hauptposition (Menge minus bereits in Lieferschein/Rechnung übernommen), nur > 0.
        Unterpositionen einer Gruppe folgen ihrer Gruppe."""
        return {p.pk: p.offen_menge for p in self.positionen.filter(gruppe__isnull=True) if p.offen_menge > 0}

    def uebernahme_engpaesse(self) -> list:
        """Bestellung → Lieferschein/Rechnung: Hauptpositionen mit Lagerartikel, deren offene Menge den verfügbaren Bestand
        (Bestand − Reservierungen anderer Bestellungen, nur dieser Mandant) übersteigt. Leer = Übernahme ohne Rückfrage."""
        from apps.lager.models import bestand, reserviert
        if self.art != Belegart.AUFTRAG:
            return []
        offen, frei, eigen, liste = self.offene_mengen(), {}, {}, []
        for p in self.positionen.select_related("artikel"):  # eigene Reservierung je Artikel (inkl. Unterpositionen)
            if p.artikel_id and p.artikel.typ == Artikeltyp.A:
                eigen[p.artikel_id] = eigen.get(p.artikel_id, Decimal("0")) + p.offen_menge
        for p in self.positionen.filter(gruppe__isnull=True).select_related("artikel"):
            if p.pk not in offen or not p.artikel_id or p.artikel.typ != Artikeltyp.A:
                continue
            a = p.artikel_id
            if a not in frei:
                fremd = reserviert(self.mandant, p.artikel) - eigen[a]
                frei[a] = max(bestand(self.mandant, p.artikel) - fremd, Decimal("0"))
            if offen[p.pk] > frei[a]:
                liste.append({"position": p, "bestellt": offen[p.pk], "verfuegbar": frei[a],
                              "bestand": bestand(self.mandant, p.artikel),
                              "reserviert": reserviert(self.mandant, p.artikel) - eigen[a]})
            frei[a] -= min(offen[p.pk], frei[a])
        return liste

    def uebernahme_mengen(self, entscheidungen=None) -> dict:
        """{Positions-ID: zu übernehmende Menge} für Lieferschein/Rechnung aus einer Bestellung (Standard: alles Offene).
        entscheidungen: {Positions-ID: Menge} für Engpass-Positionen; 0 = komplett reservieren (nichts übernehmen),
        sonst Teillieferung mit dieser Menge (höchstens verfügbar)."""
        p_ids = self.p_hauptpositionen()  # Preislistenartikel (P) werden nie übernommen
        mengen = {pk: m for pk, m in self.offene_mengen().items() if pk not in p_ids}
        if entscheidungen is not None:
            for e in self.uebernahme_engpaesse():
                pk = e["position"].pk
                if pk not in entscheidungen:
                    raise ValueError(f"Für Position {e['position'].pos} fehlt die Entscheidung (komplett reservieren oder Teillieferung).")
                menge = Decimal(entscheidungen[pk])
                if menge < 0 or menge > e["verfuegbar"]:
                    raise ValueError(f"Position {e['position'].pos}: Teillieferung höchstens {e['verfuegbar']:f} Stück.")
                mengen[pk] = menge
        return mengen

    def _uebernahme(self, art, entscheidungen):
        """Mengen je Position für die Übernahme einer Bestellung in Lieferschein/Rechnung (sonst None = alles kopieren)."""
        if self.art != Belegart.AUFTRAG or art not in (Belegart.LIEFERSCHEIN, Belegart.RECHNUNG, Belegart.PROFORMA):
            return None
        # Proforma: das Offene der Bestellung, ohne Engpass-Rückfrage (es wird noch nichts geliefert)
        p_ids = self.p_hauptpositionen()
        mengen = ({pk: m for pk, m in self.offene_mengen().items() if pk not in p_ids}
                  if art == Belegart.PROFORMA else self.uebernahme_mengen(entscheidungen))
        echte = self.echte_hauptpositionen() if p_ids else None  # mit P zählen freie Textpositionen nicht als Übernahme
        if not any(m > 0 and (echte is None or pk in echte) for pk, m in mengen.items()):
            if p_ids and not any(pk in echte and pk not in p_ids for pk in self.offene_mengen()):
                raise ValueError("Die Bestellung enthält nur noch Preislistenartikel (P) – für Lieferschein oder Rechnung ist nichts da. "
                                 "Sie bleiben in der Bestellung, bis sie im Wareneingang als eigene Artikel (A) angelegt sind; "
                                 "als Angebot weiterführen: Mehr → Kopieren als → Angebot.")
            raise ValueError("Es wird nichts übernommen: Alle Positionen bleiben komplett reserviert in der Bestellung.")
        return mengen

    def umwandlung_moeglich(self, art, mengen, p_ids) -> bool:
        """Wird beim Übernehmen der Beleg selbst umgewandelt (Angebot -> Bestellung, Bestellung -> Rechnung) statt kopiert?
        Nur bei vollständiger Übernahme; bei einer Teilübernahme (P-Artikel, Engpass/Teilmenge, schon vorhandene Folgebelege,
        Zahlungen oder Anzahlungsverrechnungen) entsteht wie bisher ein neuer Beleg und der Rest bleibt im Ursprungsbeleg."""
        if (self.art, art) not in UMWANDELBAR or p_ids or not self.pk:
            return False
        if self.folgebelege.exists():  # auch stornierte Proforma und frühere Kopien: sonst zählten deren Positionen als übernommen
            return False
        if BelegVorgang.objects.filter(quelle=self, quelle_abgeschlossen_durch_vorgang=True).exclude(beleg=self).exists():
            return False  # Kopie als Angebot noch offen: sie schließt dieses Angebot beim Speichern ab, also nicht umwandeln
        if art == Belegart.RECHNUNG:
            if mengen is None or self.zahlungen.exists() or self.anzahlung_verrechnungen.exists():
                return False
            for p in self.positionen.filter(pk__in=self.echte_hauptpositionen()):
                if mengen.get(p.pk, Decimal("0")) != p.menge:
                    return False
        return True

    def _umwandeln(self, art, vorlaeufig=False) -> "Beleg":
        """Derselbe Datensatz bekommt die neue Belegart und eine neue Nummer; die alte Nummer bleibt im Verlauf (BelegUmwandlung).
        Eine Bestellung nummeriert sofort, die Rechnung erst beim Abschließen (lückenlos). Positionen bleiben unverändert.
        vorlaeufig (Belegmaske): der umgewandelte Beleg hat noch keine Nummer; der Ausgangszustand ist im BelegVorgang gesichert,
        „Verwerfen“ wandelt ihn mit alter Art, Nummer und Datum zurück."""
        from apps.kern.middleware import aktueller_benutzer
        from . import vorgang
        with transaction.atomic():
            # Zeile sperren und Zustand neu lesen: Doppelklick/zweiter Tab darf nicht zweimal umwandeln
            aktuell = Beleg.objects.select_for_update().get(pk=self.pk)
            if (aktuell.art, art) not in UMWANDELBAR or aktuell.art != self.art or aktuell.nummer != self.nummer:
                raise ValueError(f"{self} wurde inzwischen umgewandelt – bitte den Beleg neu öffnen.")
            # Bedingungen nach der Sperre erneut prüfen: zwischenzeitlich Folgebeleg/Zahlung -> nicht umwandeln
            if (aktuell.folgebelege.exists() or aktuell.zahlungen.exists() or aktuell.anzahlung_verrechnungen.exists()):
                raise ValueError(f"{self}: Inzwischen gibt es Folgebelege oder Zahlungen – bitte den Beleg neu öffnen und erneut übernehmen.")
            if BelegVorgang.objects.filter(quelle=aktuell, quelle_abgeschlossen_durch_vorgang=True).exclude(beleg=aktuell).exists():
                raise ValueError(f"{self}: Eine nicht gespeicherte Kopie wird beim Speichern dieses Belegs abschließen – "
                                 "bitte die Kopie zuerst speichern oder verwerfen.")
            benutzer = aktueller_benutzer()
            benutzer = benutzer if benutzer is not None and benutzer.is_authenticated else None
            stand = vorgang.stand_sichern(self, umwandlung=True) if vorlaeufig else None
            if vorlaeufig:  # nicht gespeichertes Angebot: sein eigener Vorgang (OneToOne) wird im Stand gesichert und beim Verwerfen zurückgeholt
                stand["vorheriger_vorgang"] = vorgang.eigenen_vorgang_sichern(self)
            BelegUmwandlung.objects.create(beleg=self, von_art=self.art, von_nummer=self.nummer or "",
                                           von_belegdatum=self.belegdatum, nach_art=art, benutzer=benutzer)
            heute = timezone.localdate()
            if self.abo_id:  # Abo-Bestellung: nie vor dem Stichtag
                heute = max(heute, self.belegdatum)
            self.art, self.belegdatum, self.abgeschlossen = art, heute, False
            self.gueltig_bis = self.liefertermin = None
            self.vorlaeufig = vorlaeufig
            self.zuletzt_aktiv = timezone.now() if vorlaeufig else None
            self.nummer = None if vorlaeufig or art in GESPERRT_NACH_ABSCHLUSS else Nummernkreis.naechste(self.mandant, art, heute.year)
            self.save()  # nicht update(): das Änderungsprotokoll hält den Wechsel von Art und Nummer fest
            if vorlaeufig:
                vorgang.vorgang_beginnen(self, Vorgangsweg.UMWANDLUNG, stand=stand)
            self.__dict__.pop("verlauf", None)
            self.berechne()  # Summen wie beim neuen Folgebeleg frisch rechnen
        return self

    def _positionen_uebernehmen(self, neu, mengen, p_ids):
        """Bestellung → Lieferschein/Rechnung/Proforma: die übernommene Menge WANDERT aus der Bestellung in den Folgebeleg, die
        Bestellung behält nur den Rest (Verlauf: BelegUebernahme). Alle anderen Fälle sind echte Kopien."""
        if self.art == Belegart.AUFTRAG and mengen is not None:
            from . import uebernahme
            neu.uebernahmen_neu = uebernahme.menge_verschieben(self, neu, mengen)
            uebernahme.nebenkosten_mitgeben(self, neu)
        else:
            kopiere_positionen(self, neu, mengen, ausser=p_ids)

    def _nach_sperre_aktualisieren(self):
        """Beleg sperren und neu lesen. Wurde er inzwischen umgewandelt (andere Art oder Nummer), ist die Anfrage veraltet."""
        art, nummer = self.art, self.nummer
        if Beleg.objects.select_for_update().filter(pk=self.pk).first() is None:
            return
        self.refresh_from_db()
        if (self.art, self.nummer) != (art, nummer):
            raise ValueError(f"{self} wurde inzwischen umgewandelt – bitte den Beleg neu öffnen.")

    @transaction.atomic
    def folgebeleg_erzeugen(self, entscheidungen=None, art=None, vorlaeufig=False) -> "Beleg":
        """Nächste Stufe im Belegfluss. Bestellung -> Rechnung (art=RE) ist zusätzlich direkt möglich.
        Rückgabe: bei vollständiger Übernahme von Angebot/Bestellung der umgewandelte Beleg selbst, sonst der neue Beleg.
        vorlaeufig (nur die Belegmaske): der Folgebeleg hat keine Nummer und ist erst mit seinem Abschluss endgültig; die Bestellung
        wird bis dahin nicht abgeschlossen, und „Verwerfen“ bucht alles zurück."""
        from . import vorgang
        if self.pk:  # Zeile sperren, bevor der Zustand gelesen wird: Doppelklick/zweiter Tab darf nicht doppelt übernehmen
            self._nach_sperre_aktualisieren()  # aktueller Stand nach dem Warten auf die Sperre (z. B. Versandkosten schon mitgegeben)
        self.arbeitsstand_beenden()
        art = art or self.folgeart
        if not self.folgeart:
            raise ValueError("Für diese Belegart gibt es keinen Folgebeleg.")
        if art != self.folgeart and not (self.art == Belegart.AUFTRAG and art == Belegart.RECHNUNG):
            raise ValueError(f"Aus {self.get_art_display()} kann kein {Belegart(art).label} übernommen werden.")
        if self.art == Belegart.LIEFERSCHEIN and not self.abgeschlossen:
            raise ValueError("Den Lieferschein erst abschließen (bucht das Lager ab), dann die Rechnung daraus erstellen.")
        self.positionen_pruefen(art)
        self.folge_pruefen(art)
        p_ids, verschieben = self._p_vorbereiten(art)
        anzahl = self.p_positionen().count() if p_ids else 0
        mengen = self._uebernahme(art, entscheidungen)  # prüft vor dem Anlegen: nichts anlegen, wenn nichts zu übernehmen ist
        aus_vorlaeufigem_angebot = vorlaeufig and self.vorlaeufiges_angebot  # das Angebot wird erst mit dem Ziel abgeschlossen
        if self.umwandlung_moeglich(art, mengen, p_ids):
            umgewandelt = self._umwandeln(art, vorlaeufig=vorlaeufig)
            if aus_vorlaeufigem_angebot:
                vorgang.abschluss_vormerken(umgewandelt, "angebot")
            return umgewandelt
        stand = vorgang.stand_sichern(self) if vorlaeufig else None  # vor der ersten Änderung an dieser Bestellung
        neu = Beleg.objects.create(
            mandant=self.mandant, art=art, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=self.zahlungsbedingung, rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=self.versandkosten, verpackung=self.verpackung, versicherung=self.versicherung,
            rabatt_prozent=self.rabatt_prozent,
            referenz=self.referenz, vortext=self.vortext, schlusstext=self.schlusstext,
            bemerkung=self.bemerkung, vorgaenger=self, vorlaeufig=vorlaeufig, abo=self.abo,
            **({"belegdatum": max(timezone.localdate(), self.belegdatum)} if self.abo_id else {}))
        if vorlaeufig:
            vorgang.vorgang_beginnen(neu, Vorgangsweg.FOLGE, quelle=self, stand=stand)
        angebot = self._p_ins_angebot_verschieben(p_ids, ziel=neu) if verschieben else None
        self._positionen_uebernehmen(neu, mengen, p_ids)
        if self.art == Belegart.LIEFERSCHEIN:
            neu.lieferscheine.add(self)
        neu.berechne()
        self._p_abschluss(neu, anzahl, p_ids, verschieben, angebot)
        if aus_vorlaeufigem_angebot:
            vorgang.abschluss_vormerken(neu, "angebot")
        return neu


    @transaction.atomic
    def teillieferung_abschliessen(self) -> "Beleg":
        """Positionen mit ausreichendem Bestand wandern in einen neuen Beleg derselben Art, der abgeschlossen wird (nie vorläufig);
        der Rest bleibt unverändert als Entwurf in diesem Beleg. Versandkosten laufen mit der Teillieferung.
        Vorläufiger Beleg: Stammt er aus einer Bestellung, geht der Rest sofort zurück in die Bestellung (kein Rest-Entwurf);
        eine vorläufige Umwandlung Bestellung → Rechnung wird zurückgedreht und der lieferbare Teil als neue Rechnung aus der
        Bestellung übernommen; ein neuer Beleg ohne Bestellung bleibt mit dem Rest vorläufig bestehen.
        Am Ergebnis steht rest_beleg: der Beleg, der den Rest hält (None = kein Rest mehr als eigener Beleg)."""
        from apps.lager.services import teilung
        from . import vorgang
        if self.abgeschlossen:
            raise ValueError("Der Beleg ist schon abgeschlossen – eine Teillieferung gibt es nur für Entwürfe.")
        lieferbar, knapp = teilung(self)
        if not lieferbar or not knapp:
            raise ValueError("Eine Teillieferung ist nur möglich, wenn ein Teil der Positionen lieferbar ist und ein Teil nicht.")
        # Sperrreihenfolge wie beim Verwerfen: erst die Quelle (Angebot/Urbeleg), dann der Beleg
        quelle_vorab = BelegVorgang.objects.filter(beleg=self).values_list("quelle_id", flat=True).first()
        if quelle_vorab:
            Beleg.objects.select_for_update().filter(pk=quelle_vorab).first()
        Beleg.objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db(fields=["vorlaeufig"])
        v = BelegVorgang.objects.filter(beleg=self).first() if self.vorlaeufig else None
        if v is not None and v.weg == Vorgangsweg.UMWANDLUNG:
            return self._teillieferung_aus_umwandlung()
        self.arbeitsstand_beenden()
        neu = Beleg.objects.create(
            mandant=self.mandant, art=self.art, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=self.zahlungsbedingung, rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=self.versandkosten, verpackung=self.verpackung, versicherung=self.versicherung,
            rabatt_prozent=self.rabatt_prozent, bezugnahme=self.bezugnahme, liefertermin=self.liefertermin, bearbeiter=self.bearbeiter,
            referenz=self.referenz, vortext=self.vortext, schlusstext=self.schlusstext,
            bemerkung=self.bemerkung, vorgaenger=self.vorgaenger, nl_lager_abbuchen=self.nl_lager_abbuchen)
        ids = [p.pk for p in lieferbar]
        for p in Position.objects.filter(models.Q(pk__in=ids) | models.Q(gruppe_id__in=ids)):
            p.beleg = neu
            p.save()  # nicht update(): das Änderungsprotokoll hält das Umhängen fest; der Verlauf der Übernahmen folgt der Position
        Beleg.objects.filter(pk=self.pk).update(versandkosten=0, verpackung=0, versicherung=0)
        self.versandkosten = self.verpackung = self.versicherung = Decimal("0")
        self.berechne()
        neu.berechne()
        neu.abschliessen()
        neu.rest_beleg = self
        if v is not None:
            if v.angebot_id and v.angebot.vorlaeufig:  # das P-Angebot gehört zur erfolgten Übernahme: es wird gespeichert
                nummer = Nummernkreis.naechste(v.angebot.mandant, v.angebot.art, v.angebot.belegdatum.year)
                Beleg.objects.filter(pk=v.angebot_id).update(nummer=nummer, vorlaeufig=False, zuletzt_aktiv=None)
                v.angebot = None
            quelle = v.quelle
            rest_zurueck = v.weg in (Vorgangsweg.FOLGE, Vorgangsweg.KOPIE) and quelle is not None and quelle.art == Belegart.AUFTRAG
            if rest_zurueck:  # der Rest geht an die Bestellung zurück: robuster Weg, die Bestellung wird dabei nicht abgeschlossen
                stand = {k: w for k, w in v.stand.items() if k not in ("quelle_kopf", "quelle_positionen", "quelle_fingerprint")}  # die Nebenkosten bleiben bei der Teillieferung
                v.quelle_abgeschlossen_durch_vorgang, v.stand = False, stand
                v.save()
                if vorgang.beleg_verwerfen(self, None, "Teillieferung") is not None:
                    neu.rest_beleg = quelle  # der Rest steht wieder in der Bestellung
            elif v.quelle_abgeschlossen_durch_vorgang and v.stand.get("abschluss_modus") in ("urbeleg", "angebot") and quelle is not None:
                # „Ursprungsbeleg abschließen“ war gewählt (Kopie aus Angebot, Rechnung …): mit dem abgeschlossenen Teil gilt die
                # Kopie als erfolgt, der Urbeleg wird jetzt abgeschlossen – nicht erst mit dem Rest, der verworfen werden könnte.
                quelle = Beleg.objects.select_for_update().get(pk=quelle.pk)
                if not quelle.abgeschlossen:
                    quelle.abschliessen()
                v.quelle_abgeschlossen_durch_vorgang = False
                v.save()
        return neu

    def _teillieferung_aus_umwandlung(self) -> "Beleg":
        """Vorläufige Umwandlung Bestellung → Rechnung mit zu wenig Bestand: zurück zur Bestellung, der lieferbare Teil geht als
        neue, abgeschlossene Rechnung aus der Bestellung hervor (mit der Bestellung als Vorgänger und im Verlauf)."""
        from apps.lager.services import teilung
        from . import uebernahme, vorgang
        v = BelegVorgang.objects.filter(beleg=self).first()
        if v is not None and not vorgang.positionen_unveraendert(self, v.stand.get("beleg_positionen", "[]")):
            # Das Zurückwandeln stellt die Positionen der Bestellung wieder her; Eigenes aus der vorläufigen Rechnung ginge verloren
            raise ValueError("Die Positionen der Rechnung wurden seit der Übernahme aus der Bestellung geändert (Positionen, Mengen oder "
                             "Preise). Eine Teillieferung würde diese Änderungen verwerfen – bitte zuerst die Änderungen zurücknehmen "
                             "oder die Rechnung ohne Teillieferung speichern.")
        vorgang.beleg_verwerfen(self, None, "Teillieferung")
        ab = Beleg.objects.get(pk=self.pk)
        lieferbar, knapp = teilung(ab)
        if not lieferbar or not knapp:
            raise ValueError("Eine Teillieferung ist nur möglich, wenn ein Teil der Positionen lieferbar ist und ein Teil nicht.")
        mengen = {p.pk: p.offen_menge for p in lieferbar if p.offen_menge > 0}
        neu = Beleg.objects.create(
            mandant=ab.mandant, art=Belegart.RECHNUNG, kunde=ab.kunde, ansprechpartner=ab.ansprechpartner,
            zahlungsbedingung=ab.zahlungsbedingung, rechnungsanschrift=ab.rechnungsanschrift,
            versandanschrift=ab.versandanschrift, versandart=ab.versandart,
            versandkosten=ab.versandkosten, verpackung=ab.verpackung, versicherung=ab.versicherung,
            rabatt_prozent=ab.rabatt_prozent, bezugnahme=ab.bezugnahme, bearbeiter=ab.bearbeiter,
            referenz=ab.referenz, vortext=ab.vortext, schlusstext=ab.schlusstext, bemerkung=ab.bemerkung, vorgaenger=ab, abo=ab.abo,
            **({"belegdatum": max(timezone.localdate(), ab.belegdatum)} if ab.abo_id else {}))
        neu.uebernahmen_neu = uebernahme.menge_verschieben(ab, neu, mengen)
        uebernahme.nebenkosten_mitgeben(ab, neu)
        neu.berechne()
        neu.abschliessen()
        neu.rest_beleg = ab
        return neu

    @transaction.atomic
    def kopieren_als(self, art, urbeleg_abschliessen=False, entscheidungen=None, vorlaeufig=False) -> "Beleg":
        """Beliebigen Beleg als neuen Entwurf der gewünschten Belegart kopieren (Gutschrift nur über Storno).
        Optional wird der Ursprungsbeleg danach abgeschlossen (erledigt). Ausnahme: Aus einer Bestellung in Lieferschein,
        Rechnung oder Proforma wandert die übernommene Menge aus der Bestellung (keine Kopie).
        vorlaeufig (nur die Belegmaske): die Kopie hat keine Nummer; der Urbeleg wird erst mit ihrem Abschluss abgeschlossen,
        „Verwerfen“ nimmt alles zurück."""
        from . import vorgang
        if self.pk:
            self._nach_sperre_aktualisieren()  # aktueller Stand nach der Sperre (Versandkosten, Abschluss)
        self.arbeitsstand_beenden()
        if art not in Belegart.values or art in (Belegart.GUTSCHRIFT, Belegart.PROFORMA_STORNO) \
                or self.art == Belegart.PROFORMA_STORNO:
            raise ValueError("Diese Belegart kann nicht als Kopie angelegt werden.")
        if art == Belegart.LIEFERSCHEIN and self.art != Belegart.AUFTRAG:
            raise ValueError("Ein Lieferschein entsteht nur aus einer Bestellung.")
        if art == Belegart.NACHLIEFERSCHEIN and (self.art != Belegart.RECHNUNG or not self.abgeschlossen):
            raise ValueError("Ein Nachlieferschein entsteht nur aus einer abgeschlossenen Rechnung.")
        if self.art == Belegart.LIEFERSCHEIN and art == Belegart.RECHNUNG and not self.abgeschlossen:
            raise ValueError("Den Lieferschein erst abschließen (bucht das Lager ab), dann die Rechnung daraus erstellen.")
        self.positionen_pruefen(art)
        self.folge_pruefen(art)
        p_ids, verschieben = self._p_vorbereiten(art)
        anzahl = self.p_positionen().count() if p_ids else 0
        mengen = self._uebernahme(art, entscheidungen)
        nachlieferung = art == Belegart.NACHLIEFERSCHEIN
        stand = vorgang.stand_sichern(self) if vorlaeufig else None
        aus_vorlaeufigem_angebot = vorlaeufig and self.vorlaeufiges_angebot  # das Angebot wird erst mit der Kopie abgeschlossen
        neu = Beleg.objects.create(
            vorlaeufig=vorlaeufig,
            mandant=self.mandant, art=art, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=zahlungsbedingung_bezahlt() if nachlieferung else
                              zahlungsbedingung_vorkasse() if art == Belegart.PROFORMA else self.zahlungsbedingung,
            rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=0 if nachlieferung else self.versandkosten, verpackung=0 if nachlieferung else self.verpackung,
            versicherung=0 if nachlieferung else self.versicherung, rabatt_prozent=self.rabatt_prozent,
            referenz=self.referenz, vortext=self.vortext, schlusstext=self.schlusstext,
            bemerkung=f"Nachlieferung zu Rechnung {self.nummer}" if nachlieferung else self.bemerkung,
            vorgaenger=self if art != self.art else None)
        if vorlaeufig:
            vorgang.vorgang_beginnen(neu, Vorgangsweg.KOPIE, quelle=self, stand=stand)
        angebot = self._p_ins_angebot_verschieben(p_ids, ziel=neu) if verschieben else None
        self._positionen_uebernehmen(neu, mengen, p_ids)
        if self.art == Belegart.LIEFERSCHEIN and art == Belegart.RECHNUNG:
            neu.lieferscheine.add(self)
        neu.berechne()
        self._p_abschluss(neu, anzahl, p_ids, verschieben, angebot)
        if aus_vorlaeufigem_angebot:  # keine Rückfrage „Angebot abschließen?“: das Angebot schließt mit der Kopie ab
            vorgang.abschluss_vormerken(neu, "angebot")
        elif urbeleg_abschliessen and not self.abgeschlossen:
            if not (vorlaeufig and vorgang.abschluss_vormerken(neu, "urbeleg")):
                self.abschliessen()
        return neu

    @transaction.atomic
    def gutschrift_erzeugen(self, ruecknahme="", vorlaeufig=False) -> "Beleg":
        """Storno/Gutschrift zu einer abgeschlossenen Rechnung (Positionen unverändert, Belegart Gutschrift).
        ruecknahme gilt für alle Positionen und lässt sich je Position in Spalte B ändern.
        vorlaeufig (nur die Belegmaske): ohne Nummer, bis die Gutschrift abgeschlossen wird; sonst wird sie verworfen."""
        if self.art != Belegart.RECHNUNG or not self.abgeschlossen:
            raise ValueError("Eine Gutschrift gibt es nur zu einer abgeschlossenen Rechnung.")
        schon = sum((g.summe_brutto for g in self.folgebelege.filter(art=Belegart.GUTSCHRIFT)), Decimal("0"))
        if schon >= self.summe_brutto:
            raise ValueError("Zu dieser Rechnung gibt es schon Gutschriften über den vollen Betrag.")
        neu = Beleg.objects.create(
            mandant=self.mandant, art=Belegart.GUTSCHRIFT, kunde=self.kunde, ansprechpartner=self.ansprechpartner,
            zahlungsbedingung=self.zahlungsbedingung, rechnungsanschrift=self.rechnungsanschrift,
            versandanschrift=self.versandanschrift, versandart=self.versandart,
            versandkosten=self.versandkosten, verpackung=self.verpackung, versicherung=self.versicherung,
            rabatt_prozent=self.rabatt_prozent, vorlaeufig=vorlaeufig,
            referenz=self.referenz, bemerkung=f"Gutschrift zu Rechnung {self.nummer}", vorgaenger=self)
        if vorlaeufig:
            from . import vorgang
            vorgang.vorgang_beginnen(neu, Vorgangsweg.GUTSCHRIFT, quelle=self)
        kopiere_positionen(self, neu)
        neu.positionen.update(ruecknahme=ruecknahme)
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


class BelegUmwandlung(models.Model):
    """Verlauf eines umgewandelten Belegs: unter welcher Art und Nummer er früher geführt wurde
    (Angebot -> Bestellung -> Rechnung). Die alten Nummern bleiben vergeben und suchbar."""

    beleg = models.ForeignKey(Beleg, on_delete=models.CASCADE, related_name="umwandlungen")
    von_art = models.CharField("frühere Belegart", max_length=2, choices=Belegart.choices)
    von_nummer = models.CharField("frühere Belegnummer", max_length=20, blank=True)
    von_belegdatum = models.DateField("früheres Belegdatum")
    nach_art = models.CharField("neue Belegart", max_length=2, choices=Belegart.choices)
    am = models.DateTimeField("umgewandelt am", default=timezone.now)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = "Umwandlung eines Belegs"
        verbose_name_plural = "Umwandlungen von Belegen"
        ordering = ["pk"]
        indexes = [models.Index(fields=["von_art", "von_nummer"])]

    @property
    def von_art_name(self) -> str:
        return Belegart(self.von_art).label

    @property
    def nach_art_name(self) -> str:
        return Belegart(self.nach_art).label

    def __str__(self):
        return f"{self.von_art_name} {self.von_nummer or '(Entwurf)'} -> {self.nach_art_name}"


class Vorgangsweg(models.TextChoices):
    NEU = "N", "neu angelegt"
    FOLGE = "F", "Folgebeleg übernommen"
    UMWANDLUNG = "U", "Beleg umgewandelt"
    KOPIE = "K", "Beleg kopiert"
    GUTSCHRIFT = "G", "Gutschrift"
    SAMMEL = "S", "Sammelrechnung"


class BelegVorgang(models.Model):
    """Ausgangszustand eines vorläufigen Belegs: Wie sahen Quelle (und bei einer Umwandlung der Beleg selbst) vor dem Vorgang aus?
    Mit „Speichern: abschließen & Nummer vergeben“ wird der Eintrag gelöscht, dann gilt alles endgültig; „Verwerfen“ stellt den
    Ausgangszustand wieder her (vorgang.beleg_verwerfen). Der Stand (JSON) enthält: quelle_kopf, quelle_positionen (serialisiert),
    quelle_fingerprint, beleg_kopf und beleg_positionen (nur Umwandlung), seriennummern, esd, beruehrt, abschluss_modus."""

    beleg = models.OneToOneField(Beleg, on_delete=models.CASCADE, related_name="vorgang")
    weg = models.CharField("Weg", max_length=1, choices=Vorgangsweg.choices)
    quelle = models.ForeignKey(Beleg, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    angebot = models.ForeignKey(Beleg, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                help_text="Das mit entstandene Angebot für Preislistenartikel (P); es bekommt seine Nummer erst beim Abschluss.")
    quelle_abgeschlossen_durch_vorgang = models.BooleanField(
        default=False, help_text="Der Abschluss der Quelle (Bestellung erledigt, Urbeleg abschließen) ist vorgemerkt und läuft erst beim Abschluss des Ziels.")
    stand = models.JSONField("Ausgangszustand", default=dict, blank=True)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    angelegt_am = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Vorgang eines vorläufigen Belegs"
        verbose_name_plural = "Vorgänge vorläufiger Belege"

    def __str__(self):
        return f"{self.get_weg_display()}: {self.beleg}"


class BelegUebernahme(models.Model):
    """Unveränderlicher Verlauf einer Teilübernahme: Welche Menge wurde wann aus einer Bestellung in welchen Folgebeleg
    (Lieferschein, Rechnung, Proforma) verschoben, zurückgebucht oder bei der Umstellung von Altdaten übernommen?
    Der Folgebeleg wird live über die Zielposition gelesen (eine Teillieferung hängt Positionen in einen neuen Beleg um);
    die Kopien der Art, Nummer und Bezeichnung bleiben auch nach dem Löschen des Belegs lesbar."""

    UEBERNOMMEN, ZURUECK, UMGESTELLT = "U", "Z", "S"
    RICHTUNGEN = [(UEBERNOMMEN, "übernommen"), (ZURUECK, "zurückgebucht"), (UMGESTELLT, "umgestellt (Altdaten)")]

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="+")
    quelle = models.ForeignKey(Beleg, on_delete=models.SET_NULL, null=True, blank=True, related_name="uebernahmen_aus")
    quelle_art = models.CharField("Art der Bestellung", max_length=2, choices=Belegart.choices, default=Belegart.AUFTRAG)
    quelle_nummer = models.CharField("Nummer der Bestellung", max_length=20, blank=True)
    ziel = models.ForeignKey(Beleg, on_delete=models.SET_NULL, null=True, blank=True, related_name="uebernahmen_in")
    ziel_art = models.CharField("Art des Folgebelegs", max_length=2, choices=Belegart.choices)
    quellposition = models.ForeignKey("Position", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    zielposition = models.ForeignKey("Position", on_delete=models.SET_NULL, null=True, blank=True, related_name="uebernahmen")
    artikel = models.ForeignKey(Artikel, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    artikelnummer = models.CharField("Artikelnummer", max_length=60, blank=True)
    bezeichnung = models.CharField("Bezeichnung", max_length=255, blank=True)
    menge = models.DecimalField("Menge", max_digits=10, decimal_places=3)
    richtung = models.CharField("Richtung", max_length=1, choices=RICHTUNGEN, default=UEBERNOMMEN)
    am = models.DateTimeField("am", default=timezone.now)
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = "Übernahme aus einer Bestellung"
        verbose_name_plural = "Übernahmen aus Bestellungen"
        ordering = ["pk"]

    @property
    def vorzeichen(self) -> int:
        return -1 if self.richtung == self.ZURUECK else 1

    @property
    def ziel_beleg(self):
        """Folgebeleg, in dem die Position heute liegt (None = Beleg gelöscht)."""
        if self.zielposition_id:
            return self.zielposition.beleg
        return self.ziel

    @property
    def ziel_text(self) -> str:
        beleg = self.ziel_beleg
        if beleg is not None:
            return f"{beleg.get_art_display()} {beleg.nummer or ('(nicht gespeichert)' if beleg.vorlaeufig else '(Entwurf)')}"
        return f"{Belegart(self.ziel_art).label} (gelöscht)"

    @property
    def text(self) -> str:
        """Zeile für Anzeige und Druck, z. B. „7 × Maus übernommen in Rechnung (Entwurf)“."""
        menge = f"{self.menge.normalize():f}".replace(".", ",")
        name = (f"{self.artikelnummer} " if self.artikelnummer else "") + self.bezeichnung
        if self.richtung == self.ZURUECK:
            return f"{menge} × {name} zurückgebucht aus {self.ziel_text}"
        return f"{menge} × {name} übernommen in {self.ziel_text}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Der Verlauf der Übernahmen ist unveränderlich.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Der Verlauf der Übernahmen ist unveränderlich.")

    def __str__(self):
        return self.text


class Mailvorlage(Zeitstempel):
    """Betreff und Text je Mandant und Mailart. Platzhalter je Art: PLATZHALTER in mail.py"""

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT)
    art = models.CharField("Art", max_length=2, choices=Mailart.choices)
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


def zahlungsbedingung_vorkasse():
    """Zahlungsbedingung jeder Proforma-Rechnung, unabhängig vom Kunden: die als Vorkasse markierte Bedingung
    (bei mehreren die mit der Bezeichnung „Vorkasse“), sonst wird „Vorkasse“ angelegt."""
    from apps.stammdaten.models import Zahlungsbedingung
    alle = list(Zahlungsbedingung.objects.filter(vorkasse=True).order_by("pk"))
    for zb in alle:
        if zb.bezeichnung.strip().lower() == "vorkasse":
            return zb
    if alle:
        return alle[0]
    zb, _ = Zahlungsbedingung.objects.get_or_create(
        bezeichnung="Vorkasse", defaults={"druck_text": "Zahlbar im Voraus. Die Lieferung erfolgt nach Zahlungseingang.",
                                          "tage_netto": 0, "vorkasse": True})
    if not zb.vorkasse:  # gleichnamige Bedingung ohne Markierung
        Zahlungsbedingung.objects.filter(pk=zb.pk).update(vorkasse=True)
        zb.vorkasse = True
    return zb


def kopiere_positionen(quelle, ziel, mengen=None, ausser=()):
    """Kopiert alle Positionen samt Gruppenzuordnung in einen anderen Beleg. Stammt die Quelle aus einem
    Lieferschein, bleibt der Lieferschein an der Position vermerkt (Nummer und Datum im Ausdruck).
    mengen ({Positions-ID: Menge}, nur Hauptpositionen): übernimmt nur diese Positionen mit dieser Menge;
    Unterpositionen folgen ihrer Gruppe. ausser: IDs von Hauptpositionen (samt Unterpositionen), die nicht kopiert werden."""
    neu = {}
    lieferschein = quelle if quelle.art == Belegart.LIEFERSCHEIN else None
    for p in quelle.positionen.all():
        menge = p.menge
        if p.gruppe_id:
            if (mengen is not None or ausser) and p.gruppe_id not in neu:
                continue
        else:
            if p.pk in ausser:
                continue
            if mengen is not None:
                menge = mengen.get(p.pk, Decimal("0"))
                if menge <= 0:
                    continue
        neu[p.pk] = Position.objects.create(
            beleg=ziel, gruppe=neu.get(p.gruppe_id), typ=p.typ, artikel=p.artikel, bezeichnung=p.bezeichnung,
            preisliste_artikel=p.preisliste_artikel, menge=menge, einzelpreis=p.einzelpreis, rabatt_prozent=p.rabatt_prozent,
            steuer_prozent=p.steuer_prozent, zusatztext=p.zusatztext, lieferschein=lieferschein or p.lieferschein,
            quellposition=p, preis_fest=p.preis_fest)


UEBERNAHME_ARTEN = (Belegart.LIEFERSCHEIN, Belegart.RECHNUNG, Belegart.PROFORMA)  # nur diese Folgebelege zählen als Teilübernahme


def stornierte_proformas():
    """Abfrage der stornierten Proformas: Ihre Positionen gelten nicht als aus der Bestellung übernommen."""
    return Beleg.objects.filter(art=Belegart.PROFORMA, folgebelege__art=Belegart.PROFORMA_STORNO).values("pk")


def mit_offener_menge(positionen):
    """Annotiert Positionen mit `offen`: Menge minus Menge, die in Folgebelege (Lieferschein/Rechnung …) übernommen wurde.
    Verschobene Mengen zählen nicht: sie sind schon aus der Bestellmenge herausgenommen (nur alte Kopien werden abgezogen)."""
    uebernommen = (Position.objects.filter(quellposition=OuterRef("pk"), beleg__vorgaenger_id=OuterRef("beleg_id"), verschoben=False,
                                           beleg__art__in=UEBERNAHME_ARTEN)
                   .exclude(beleg__in=stornierte_proformas()).order_by().values("quellposition").annotate(s=Sum("menge")).values("s"))
    dez = models.DecimalField(max_digits=10, decimal_places=3)
    return positionen.annotate(offen=F("menge") - Coalesce(Subquery(uebernommen, output_field=dez), Value(Decimal("0")), output_field=dez))


@transaction.atomic
def rechnung_aus_lieferscheinen(lieferscheine, vorlaeufig=False) -> Beleg:
    """Mehrere Lieferscheine eines Kunden zu einer Rechnung zusammenfassen. vorlaeufig (nur die Belegmaske): ohne Nummer,
    bis die Rechnung abgeschlossen wird; Verwerfen gibt die Lieferscheine wieder zum Abrechnen frei."""
    lieferscheine = sorted(lieferscheine, key=lambda b: (b.belegdatum, b.pk))
    if not lieferscheine:
        raise ValueError("Keine Lieferscheine ausgewählt.")
    erster = lieferscheine[0]
    for ls in lieferscheine:
        if ls.mandant_id != erster.mandant_id or ls.kunde_id != erster.kunde_id:
            raise ValueError("Alle Lieferscheine müssen zum selben Kunden und Mandanten gehören.")
        if not ls.abrechenbar:
            raise ValueError(f"{ls} ist nicht abrechenbar (nicht abgeschlossen oder schon berechnet).")
    if len({ls.rabatt_prozent for ls in lieferscheine}) > 1:
        raise ValueError("Die Lieferscheine haben unterschiedlichen Belegrabatt – bitte einzeln abrechnen.")
    rechnung = Beleg.objects.create(
        mandant=erster.mandant, art=Belegart.RECHNUNG, kunde=erster.kunde, ansprechpartner=erster.ansprechpartner,
        zahlungsbedingung=erster.zahlungsbedingung or erster.kunde.zahlungsbedingung,
        rechnungsanschrift=erster.rechnungsanschrift, versandanschrift=erster.versandanschrift,
        versandart=erster.versandart, rabatt_prozent=erster.rabatt_prozent, vorgaenger=erster, vorlaeufig=vorlaeufig,
        abo_id=erster.abo_id if len({ls.abo_id for ls in lieferscheine}) == 1 else None,  # gehören alle zum selben Abo, zählt die Rechnung zu dessen Zeitraum
        bemerkung="Lieferscheine: " + ", ".join(ls.nummer for ls in lieferscheine))
    if vorlaeufig:
        from . import vorgang
        vorgang.vorgang_beginnen(rechnung, Vorgangsweg.SAMMEL)
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
    ruecknahme = models.CharField("Bestand (Gutschrift)", max_length=1, blank=True, choices=Ruecknahme.choices)
    verschoben = models.BooleanField(
        "aus der Bestellung verschoben", default=False, editable=False,
        help_text="Die Menge dieser Position im Lieferschein, in der Rechnung oder Proforma wurde aus der Bestellung herausgenommen "
                  "(die Bestellung behält nur den Rest). Alte Kopien (False) lassen die Bestellmenge unverändert.")

    class Meta:
        verbose_name = "Position"
        verbose_name_plural = "Positionen"
        # Die Gruppe steht vor ihren Unterpositionen, auch wenn eine ältere Position nachträglich in sie verschoben wurde
        ordering = ["pos", models.F("gruppe").asc(nulls_first=True), "id"]

    @property
    def uebernommen(self) -> Decimal:
        """Menge alter Kopien (Altdaten), die noch von der Bestellmenge abzuziehen sind. Neu übernommene Mengen wandern aus der
        Bestellung heraus (Position.verschoben) und stehen im Verlauf (BelegUebernahme)."""
        if not self.pk:
            return Decimal("0")
        return Position.objects.filter(quellposition=self, beleg__vorgaenger_id=self.beleg_id, verschoben=False,
                                       beleg__art__in=UEBERNAHME_ARTEN).exclude(
            beleg__in=stornierte_proformas()).aggregate(s=Sum("menge"))["s"] or Decimal("0")

    @property
    def offen_menge(self) -> Decimal:
        """Noch nicht gelieferte/berechnete Menge einer Bestellposition (Rückstand)."""
        return max(self.menge - self.uebernommen, Decimal("0"))

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
                    if e is None and t.typ == "L":  # Leistung ohne Kosten in der Gruppe: zählt mit 0 € EK
                        continue
                    if e is None:
                        return None
                    summe += e * t.menge
                return (summe / self.menge) if self.menge else None
        if self.preisliste_artikel_id:
            return self.preisliste_artikel.ek
        if self.artikel_id and self.artikel.typ in ("A", "G", "L"):
            return self.artikel.ek_errechnet
        return None

    @property
    def netto_gesamt(self) -> Decimal:
        if self.gruppe_id:  # Unterartikel einer Gruppe: der Preis steht an der Gruppenposition
            return Decimal("0.00")
        return runden(self.menge * self.einzelpreis * (Decimal("100") - self.rabatt_prozent) / 100)

    def save(self, *args, **kwargs):
        if self.beleg.positionen_gesperrt:
            raise BelegGesperrt(f"{self.beleg} ist abgeschlossen – Positionen lassen sich nicht mehr ändern.")
        if not self.pk and not self.pos:
            letzte = Position.objects.filter(beleg=self.beleg).aggregate(m=models.Max("pos"))["m"] or 0
            self.pos = letzte + 1
        if not self.pk and not self.ruecknahme and self.beleg.art == Belegart.GUTSCHRIFT:  # neue Position folgt der Gutschrift
            erste = Position.objects.filter(beleg=self.beleg).order_by("pk").values_list("ruecknahme", flat=True).first()
            self.ruecknahme = Ruecknahme.REPARATUR if erste is None else erste
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.beleg.positionen_gesperrt:
            raise BelegGesperrt(f"{self.beleg} ist abgeschlossen – Positionen lassen sich nicht mehr ändern.")
        super().delete(*args, **kwargs)


class BelegArbeitsstand(models.Model):
    """Veraltet: Positionsänderungen gelten sofort, neue Arbeitsstände entstehen nicht mehr. Das Modell bleibt vorerst bestehen
    (Altdaten werden beim Öffnen des Belegs still beendet); Entfernen in einem späteren Schritt."""
    beleg = models.OneToOneField(Beleg, on_delete=models.CASCADE, related_name="arbeitsstand")
    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    daten = models.TextField("Positionen vor der Änderung (JSON)")
    angelegt_am = models.DateTimeField(auto_now_add=True)
    geaendert_am = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Arbeitsstand eines Belegs"
        verbose_name_plural = "Arbeitsstände von Belegen"


def monate_addieren(tag: date, monate: int) -> date:
    """Datum um ganze Monate verschieben; der Tag wird bei kurzen Monaten auf den Monatsletzten gekürzt."""
    index = tag.year * 12 + tag.month - 1 + monate
    jahr, monat = divmod(index, 12)
    return date(jahr, monat + 1, min(tag.day, calendar.monthrange(jahr, monat + 1)[1]))


class Abo(Zeitstempel):
    """Wiederkehrende Rechnung (Wartungsvertrag, Hosting, Miete …): erzeugt zum Termin eine Bestellung (mit Nummer, nie
    automatisch verworfen), aus der von Hand die Rechnung wird."""

    INTERVALLE = [(1, "monatlich"), (3, "vierteljährlich"), (6, "halbjährlich"), (12, "jährlich")]
    HALBMONATLICH = 15  # keine Monatszahl: Rechnung am 15. und am Monatsletzten, Mengen nach Mengenregel
    RHYTHMEN = [(HALBMONATLICH, "halbmonatlich (15. und Monatsende)"), *INTERVALLE]

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="abos")
    kunde = models.ForeignKey(Kunde, on_delete=models.PROTECT, related_name="abos")
    bezeichnung = models.CharField("Bezeichnung", max_length=100, help_text="z. B. Wartungsvertrag 2026")
    monate = models.PositiveSmallIntegerField("Rhythmus", choices=RHYTHMEN, default=1)
    naechster_termin = models.DateField("nächste Rechnung am")
    ende = models.DateField("Ende (optional)", null=True, blank=True)
    aktiv = models.BooleanField(default=True)
    starttag = models.PositiveSmallIntegerField("Tag im Monat", null=True, blank=True, editable=False,
                                                help_text="Gewünschter Tag; bei kurzen Monaten wird auf den Monatsletzten gekürzt, ohne dass der Tag wandert.")
    rabatt_prozent = models.DecimalField("Belegrabatt %", max_digits=5, decimal_places=2, default=Decimal("0"))
    versandkosten = models.DecimalField("Versandkosten netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    verpackung = models.DecimalField("Verpackung netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    versicherung = models.DecimalField("Versicherung netto", max_digits=12, decimal_places=2, default=Decimal("0"))
    zahlungsbedingung = models.ForeignKey(Zahlungsbedingung, on_delete=models.PROTECT, null=True, blank=True)
    vortext = models.TextField("Text vor den Positionen", blank=True)
    schlusstext = models.TextField("Text nach den Positionen", blank=True)
    letzter_lauf = models.DateField(null=True, blank=True, editable=False)
    letzte_rechnung = models.ForeignKey("Beleg", on_delete=models.SET_NULL, null=True, blank=True, editable=False, related_name="+",
                                        verbose_name="letzter Beleg")

    class Meta:
        verbose_name = "Wiederkehrende Rechnung"
        verbose_name_plural = "Wiederkehrende Rechnungen"
        ordering = ["naechster_termin", "kunde"]

    def __str__(self):
        return f"{self.bezeichnung} – {self.kunde.name1} ({self.get_monate_display()})"

    def clean(self):
        """Abgerechnet wird nachträglich je Halbmonat; ein Ende dazwischen ließe die letzten Tage unberechnet."""
        if self.halbmonatlich and self.ende and self.ende != stichtag(self.ende):
            raise ValidationError({"ende": "Halbmonatlich muss das Ende ein Stichtag sein (15. oder Monatsletzter)."})

    def save(self, *args, **kwargs):
        if self.halbmonatlich:
            self.naechster_termin = stichtag(self.naechster_termin)
        super().save(*args, **kwargs)

    @property
    def halbmonatlich(self) -> bool:
        return self.monate == self.HALBMONATLICH

    def zeilen(self, termin=None) -> list:
        """(Position, Menge, Zusatztext) der Rechnung zum Termin. Halbmonatlich zählen die Tage des Halbmonats, bei
        „nur am Monatsende“ die des ganzen Monats; was auf 0 kommt, entfällt."""
        if not self.halbmonatlich:
            return [(p, p.menge, "") for p in self.positionen.all()]
        termin = termin or self.naechster_termin
        zeilen = []
        for p in self.positionen.all():
            if p.nur_monatsende and termin.day <= 15:
                continue
            von, bis = ganzer_monat(termin) if p.nur_monatsende else halbmonat(termin)
            menge = p.menge_im_zeitraum(von, bis)
            if menge:
                zeilen.append((p, menge, f"Abrechnungszeitraum {von:%d.%m.%Y} - {bis:%d.%m.%Y}"))
        return zeilen

    @property
    def netto(self):
        return sum((menge * p.einzelpreis * (1 - p.rabatt_prozent / 100) for p, menge, _ in self.zeilen()), Decimal("0"))

    def offener_beleg(self):
        """Der Beleg des laufenden Abo-Zeitraums, solange er nicht erledigt ist: Bestellung mit offenen Mengen, nicht abgeschlossene
        Rechnung (auch aus Altdaten oder durch Umwandlung) oder ein nicht abgeschlossener Folgebeleg mit dieser Wiederkehr.
        None = nichts offen. Der Cron prüft das bewusst nicht (je Zeitraum ein Beleg)."""
        kandidaten = [self.letzte_rechnung] if self.letzte_rechnung_id else []
        kandidaten += list(Beleg.objects.filter(abo=self, abgeschlossen=False).exclude(pk=self.letzte_rechnung_id).order_by("-pk"))
        for b in kandidaten:
            if b.abgeschlossen:
                continue
            if b.art == Belegart.AUFTRAG and not b.offene_mengen():
                continue
            return b
        # Vollständig geliefert, aber noch nicht abgerechnet: der abgeschlossene Lieferschein zählt als offener Zeitraum, solange er
        # in keiner Rechnung steht oder nur in einer, die noch nicht abgeschlossen ist (Sammelrechnung, Entwurf)
        if self.letzte_rechnung_id:
            for ls in Beleg.objects.filter(abo=self, art=Belegart.LIEFERSCHEIN, abgeschlossen=True,
                                           vorgaenger_id=self.letzte_rechnung_id).order_by("-pk"):
                if ls.abrechenbar:
                    return ls
                offene_re = ls.rechnungen.filter(abgeschlossen=False).first() or ls.folgebelege.filter(art=Belegart.RECHNUNG, abgeschlossen=False).first()
                if offene_re is not None:
                    return offene_re
        return None

    @transaction.atomic
    def rechnung_erzeugen(self, heute=None):
        """Legt die Bestellung an (mit Bestellnummer, nicht vorläufig; Rechnung und Lieferschein entstehen daraus von Hand), rückt
        den Termin weiter. Halbmonatlich kann ein Stichtag leer bleiben (nur Positionen zum Monatsende): Dann rückt nur der Termin,
        Rückgabe None."""
        from apps.verkauf.views import adresse_text
        from apps.stammdaten.models import Anschriftart
        heute = heute or timezone.localdate()
        termin = self.naechster_termin
        zeilen = self.zeilen(termin)
        if not zeilen and self.halbmonatlich and self.positionen.exists():
            self._termin_weiter(termin)
            self.save()
            return None
        if not zeilen:
            raise ValueError(f"{self}: keine Positionen hinterlegt.")
        beleg = Beleg.objects.create(
            mandant=self.mandant, art=Belegart.AUFTRAG, abo=self, kunde=self.kunde,
            belegdatum=max(heute, termin) if self.halbmonatlich else heute,
            ansprechpartner=self.kunde.ansprechpartner.filter(hauptkontakt=True).first(),
            zahlungsbedingung=self.zahlungsbedingung or self.kunde.zahlungsbedingung,
            rechnungsanschrift=adresse_text(self.kunde, Anschriftart.RECHNUNG),
            versandanschrift=adresse_text(self.kunde, Anschriftart.VERSAND),
            rabatt_prozent=self.rabatt_prozent, versandkosten=self.versandkosten, verpackung=self.verpackung,
            versicherung=self.versicherung,
            vortext=self.vortext, schlusstext=self.schlusstext, bemerkung=f"Wiederkehrende Bestellung: {self.bezeichnung}")
        for p, menge, zusatztext in zeilen:
            Position.objects.create(
                beleg=beleg, typ=("L" if not p.artikel_id or p.artikel.typ == "L" else "A"), artikel=p.artikel, bezeichnung=p.bezeichnung,
                zusatztext=zusatztext, menge=menge, einzelpreis=p.einzelpreis, rabatt_prozent=p.rabatt_prozent, steuer_prozent=p.steuer_prozent)
        beleg.berechne()
        self.letzter_lauf, self.letzte_rechnung = heute, beleg
        self._termin_weiter(termin)
        self.save()
        return beleg

    def _termin_weiter(self, termin):
        if self.halbmonatlich:
            self.naechster_termin = naechster_stichtag(termin)
        else:
            wunschtag = self.starttag or termin.day
            weiter = monate_addieren(termin.replace(day=1), self.monate)
            self.naechster_termin = weiter.replace(day=min(wunschtag, calendar.monthrange(weiter.year, weiter.month)[1]))
        if self.ende and self.naechster_termin > self.ende:
            self.aktiv = False


class Mengenregel(models.TextChoices):
    """Woraus sich die Menge bei halbmonatlicher Abrechnung ergibt."""
    FEST = "F", "feste Menge"
    WOCHENTAGE = "W", "Wochentage (Mo–Fr ohne Feiertag)"
    FREI = "E", "Wochenende und Feiertage"
    ALLE = "A", "alle Kalendertage"


class AboPosition(models.Model):
    abo = models.ForeignKey(Abo, on_delete=models.CASCADE, related_name="positionen")
    artikel = models.ForeignKey(Artikel, on_delete=models.PROTECT, null=True, blank=True)
    bezeichnung = models.CharField("Bezeichnung", max_length=255)
    menge = models.DecimalField("Menge", max_digits=10, decimal_places=3, default=Decimal("1"),
                                help_text="Gilt bei der Mengenregel „feste Menge“.")
    regel = models.CharField("Mengenregel", max_length=1, choices=Mengenregel.choices, default=Mengenregel.FEST,
                             help_text="Nur bei halbmonatlichem Rhythmus: Die Menge zählt das Programm aus dem Kalender (Feiertage NRW).")
    nur_monatsende = models.BooleanField("nur am Monatsende", default=False,
                                         help_text="Nur bei halbmonatlichem Rhythmus: Die Position steht in der Rechnung zum Monatsende, Zeitraum ist der ganze Monat.")
    einzelpreis = models.DecimalField("Einzelpreis netto", max_digits=12, decimal_places=2)
    rabatt_prozent = models.DecimalField("Rabatt %", max_digits=5, decimal_places=2, default=Decimal("0"))
    steuer_prozent = models.DecimalField("Steuer %", max_digits=5, decimal_places=2, default=Decimal("19"))

    class Meta:
        verbose_name = "Position"
        verbose_name_plural = "Positionen"
        ordering = ["id"]

    def menge_im_zeitraum(self, von, bis) -> Decimal:
        wochentage, frei = tage_zaehlen(von, bis)
        tage = {Mengenregel.WOCHENTAGE: wochentage, Mengenregel.FREI: frei, Mengenregel.ALLE: wochentage + frei}.get(self.regel)
        return self.menge if tage is None else Decimal(tage)


ZEITRAUM = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})")


def regel_vorschlag(menge, zusatztext) -> tuple:
    """(Mengenregel, nur am Monatsende) für eine Rechnungsposition: Der Zeitraum im Zusatztext und die Menge werden
    mit dem Kalender verglichen. Ein ganzer Monat zählt für die Rechnung zum Monatsende; stimmt die Menge dort mit allen
    Tagen der zweiten Monatshälfte überein, läuft die Position in beiden Hälften. Was nicht aufgeht, bleibt feste Menge."""
    treffer = ZEITRAUM.search(zusatztext or "")
    if not treffer:
        return Mengenregel.FEST, False
    try:
        von, bis = (datetime.strptime(t, "%d.%m.%Y").date() for t in treffer.groups())
    except ValueError:
        return Mengenregel.FEST, False
    if (von, bis) == ganzer_monat(von):
        if menge == sum(tage_zaehlen(*halbmonat(bis))):
            return Mengenregel.ALLE, False
        wochentage, frei = tage_zaehlen(von, bis)
        regel = {wochentage: Mengenregel.WOCHENTAGE, frei: Mengenregel.FREI, wochentage + frei: Mengenregel.ALLE}.get(menge, Mengenregel.FEST)
        return regel, True
    if (von, bis) == halbmonat(von):
        wochentage, frei = tage_zaehlen(von, bis)
        return {wochentage: Mengenregel.WOCHENTAGE, frei: Mengenregel.FREI, wochentage + frei: Mengenregel.ALLE}.get(menge, Mengenregel.FEST), False
    return Mengenregel.FEST, False


def faellige_abos(heute=None):
    heute = heute or timezone.localdate()
    return Abo.objects.filter(aktiv=True, naechster_termin__lte=heute).filter(
        models.Q(ende__isnull=True) | models.Q(ende__gte=models.F("naechster_termin"))).select_related("kunde", "mandant")


def letzte_preise(mandant, kunde, artikel_ids=(), preisliste_ids=(), ohne_beleg=None) -> dict:
    """Was der Kunde zuletzt bezahlt hat: Netto-Stückpreis nach Positions- und Belegrabatt aus der jüngsten abgeschlossenen
    Rechnung. Gutgeschrieben gilt als nicht bezahlt: je Artikel die gutgeschriebene Menge, außerdem Rechnungen, deren
    Gutschriften zusammen den Betrag erreichen. Schlüssel ("a", artikel_id) bzw. ("p", preislistenartikel_id)."""
    artikel_ids, preisliste_ids = [i for i in artikel_ids if i], [i for i in preisliste_ids if i]
    if not artikel_ids and not preisliste_ids:
        return {}
    gutschriften = Beleg.objects.filter(mandant=mandant, kunde=kunde, art=Belegart.GUTSCHRIFT, abgeschlossen=True, vorgaenger__isnull=False)
    summen, zurueck = {}, {}
    for rechnung, brutto in gutschriften.values_list("vorgaenger_id", "summe_brutto"):
        summen[rechnung] = summen.get(rechnung, Decimal("0")) + brutto
    for rechnung, art_id, pl_id, menge in Position.objects.filter(beleg__in=gutschriften, menge__gt=0).values_list(
            "beleg__vorgaenger_id", "artikel_id", "preisliste_artikel_id", "menge"):
        ziel = (rechnung, ("a", art_id) if art_id else ("p", pl_id))
        zurueck[ziel] = zurueck.get(ziel, Decimal("0")) + menge
    zeilen = Position.objects.filter(
        models.Q(artikel_id__in=artikel_ids) | models.Q(preisliste_artikel_id__in=preisliste_ids, artikel__isnull=True),
        beleg__mandant=mandant, beleg__kunde=kunde, beleg__art=Belegart.RECHNUNG, beleg__abgeschlossen=True,
        gruppe__isnull=True, menge__gt=0).select_related("beleg")
    if ohne_beleg:
        zeilen = zeilen.exclude(beleg_id=ohne_beleg)
    ergebnis = {}
    for p in zeilen.order_by("-beleg__belegdatum", "-beleg_id", "-id"):
        schluessel = ("a", p.artikel_id) if p.artikel_id else ("p", p.preisliste_artikel_id)
        if p.beleg_id in summen and summen[p.beleg_id] >= p.beleg.summe_brutto - Decimal("0.01"):
            continue  # Rechnung vollständig gutgeschrieben
        rest = zurueck.get((p.beleg_id, schluessel), Decimal("0"))
        if rest > 0:  # Menge dieser Zeile zurückgegeben; nur ein Rest gilt weiter als bezahlt
            zurueck[(p.beleg_id, schluessel)] = rest - p.menge
            if rest >= p.menge:
                continue
        if schluessel not in ergebnis:
            faktor = (100 - p.rabatt_prozent) * (100 - p.beleg.rabatt_prozent) / 10000
            ergebnis[schluessel] = {"preis": (p.einzelpreis * faktor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                                    "datum": p.beleg.belegdatum, "nummer": p.beleg.nummer, "beleg_id": p.beleg_id}
    return ergebnis


class Briefprodukt(models.Model):
    """Brief-Produkt der Deutschen Post für die INTERNETMARKE (Produktcode aus der Produkt-/Preisliste, Porto in Euro).
    Bei einer Portoerhöhung hier den Preis anpassen – die Post lehnt sonst den Kauf ab."""

    name = models.CharField("Bezeichnung", max_length=60)
    produktcode = models.PositiveIntegerField("Produktcode (INTERNETMARKE)", unique=True)
    preis = models.DecimalField("Porto (€)", max_digits=6, decimal_places=2)
    max_gewicht_g = models.PositiveIntegerField("Höchstgewicht (g)")
    masse = models.CharField("Maße", max_length=80, blank=True, help_text="nur zur Anzeige, z. B. bis 23,5 × 12,5 × 0,5 cm")
    aktiv = models.BooleanField("aktiv", default=True)

    class Meta:
        verbose_name = "Briefprodukt (Deutsche Post)"
        verbose_name_plural = "Briefprodukte (Deutsche Post)"
        ordering = ["max_gewicht_g"]

    def __str__(self):
        return f"{self.name} ({self.preis:.2f} €)".replace(f"{self.preis:.2f}", f"{self.preis:.2f}".replace(".", ","))

    @classmethod
    def vorschlag(cls, gewicht_kg, vollstaendig=True):
        """Kleinstes aktives Produkt, das das Gewicht trägt; ohne verlässliches Gewicht das kleinste."""
        produkte = list(cls.objects.filter(aktiv=True))
        if vollstaendig and gewicht_kg:
            for p in produkte:
                if gewicht_kg * 1000 <= p.max_gewicht_g:
                    return p
            return None
        return produkte[0] if produkte else None


class Versand(Zeitstempel):
    """Sendung zu einem Beleg (Lieferschein, Rechnung, Nachlieferschein): DHL-Paket oder Brief der Deutschen Post
    (INTERNETMARKE), mit Etikett und Versand-Mail."""

    DIENSTE = [("dhl", "DHL Paket"), ("dp", "Deutsche Post Brief")]
    TRACKING = {"dhl": "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html?piececode={nr}"}
    PAKET, KLEINPAKET = "paket", "kleinpaket"
    PRODUKTE = [(PAKET, "DHL Paket (ab 1 kg)"), (KLEINPAKET, "DHL Kleinpaket / Warenpost (bis 1 kg)")]
    KLEINPAKET_MAX_KG = Decimal("1")
    PAKET_MIN_KG = Decimal("1")

    beleg = models.ForeignKey(Beleg, on_delete=models.CASCADE, related_name="versendungen")
    dienst = models.CharField("Versanddienstleister", max_length=10, choices=DIENSTE, default="dhl")
    produkt = models.CharField("Produkt", max_length=12, choices=PRODUKTE, default=PAKET)
    sendungsnummer = models.CharField("Sendungsnummer", max_length=40, blank=True)
    gewicht_kg = models.DecimalField("Gewicht (kg)", max_digits=7, decimal_places=2, null=True, blank=True)
    versendet_am = models.DateField("Versanddatum", default=timezone.localdate)
    mail_gesendet = models.DateTimeField("Versand-Mail gesendet", null=True, blank=True, editable=False)
    dhl_erzeugt = models.BooleanField("Label über die DHL-Schnittstelle erzeugt", default=False, editable=False)
    label = models.BinaryField("DHL-Label (PDF)", null=True, blank=True, editable=False)
    # Brief (INTERNETMARKE): gekauftes Produkt, Porto und Bestellnummer der Post (für Erstattung und erneuten Abruf)
    briefprodukt = models.ForeignKey(Briefprodukt, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    porto = models.DecimalField("Porto (€)", max_digits=6, decimal_places=2, null=True, blank=True, editable=False)
    shop_order_id = models.CharField("Bestellnummer INTERNETMARKE", max_length=20, blank=True, editable=False)
    label_link = models.URLField("Link zum Etikett", max_length=500, blank=True, editable=False)
    erstattung_beantragt = models.DateTimeField("Erstattung beantragt", null=True, blank=True, editable=False)
    gedruckt_am = models.DateTimeField("Etikett zuletzt geöffnet/gedruckt", null=True, blank=True, editable=False)
    LAEUFT, UNKLAR = "laeuft", "unklar"
    kauf_status = models.CharField("Kaufstatus", max_length=8, blank=True, editable=False,
                                   choices=[(LAEUFT, "Kauf läuft"), (UNKLAR, "Kauf unklar – Portokasse prüfen")])

    class Meta:
        verbose_name = "Versand"
        verbose_name_plural = "Versendungen"
        ordering = ["-versendet_am", "-id"]

    def __str__(self):
        return f"{self.get_dienst_display()} {self.sendungsnummer or '(ohne Nummer)'}"

    @property
    def etikett_name(self) -> str:
        if self.dienst == "dp":
            return "Etikett"
        return "DHL-Label" if self.dhl_erzeugt else "Aufkleber"

    @property
    def bezeichnung(self) -> str:
        if self.dienst == "dp":
            return self.briefprodukt.name if self.briefprodukt else "Brief"
        return self.get_produkt_display()

    @property
    def tracking_url(self) -> str:
        vorlage = self.TRACKING.get(self.dienst)
        return vorlage.format(nr=self.sendungsnummer) if vorlage and self.sendungsnummer else ""

    @classmethod
    def produkt_vorschlag(cls, gewicht, inland=True, vollstaendig=True) -> str:
        """Bis 1 kg Kleinpaket (Warenpost) – aber nur im Inland und nur, wenn alle Artikel ein Gewicht haben;
        sonst DHL Paket (sicherer als ein zu leicht angemeldetes Kleinpaket)."""
        if inland and vollstaendig and gewicht and 0 < gewicht <= cls.KLEINPAKET_MAX_KG:
            return cls.KLEINPAKET
        return cls.PAKET

    @classmethod
    def versandgewicht(cls, produkt, gewicht) -> Decimal:
        """Gewicht, das an DHL geht: Paket beginnt bei 1 kg, Kleinpaket höchstens 1 kg."""
        if produkt == cls.KLEINPAKET:
            if not gewicht or gewicht <= 0:
                raise ValueError("Für das Kleinpaket bitte das Gewicht angeben (höchstens 1 kg).")
            if gewicht > cls.KLEINPAKET_MAX_KG:
                raise ValueError(f"Kleinpaket/Warenpost ist höchstens 1 kg – das Paket wiegt {gewicht:f} kg. Bitte DHL Paket wählen.")
            return gewicht
        return max(gewicht or cls.PAKET_MIN_KG, cls.PAKET_MIN_KG)

class Wiedervorlage(Zeitstempel):
    """Erinnerung an einen Kunden (optional zu einem Beleg); fällige erscheinen auf der Startseite."""

    mandant = models.ForeignKey(Mandant, on_delete=models.PROTECT, related_name="wiedervorlagen")
    kunde = models.ForeignKey(Kunde, on_delete=models.CASCADE, related_name="wiedervorlagen")
    beleg = models.ForeignKey(Beleg, on_delete=models.SET_NULL, null=True, blank=True, related_name="wiedervorlagen")
    faellig = models.DateField("fällig am")
    text = models.CharField("Notiz", max_length=300)
    erledigt = models.BooleanField(default=False, db_index=True)
    erledigt_am = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = "Wiedervorlage"
        verbose_name_plural = "Wiedervorlagen"
        ordering = ["faellig", "id"]

    def __str__(self):
        return f"{self.faellig:%d.%m.%Y} {self.kunde} – {self.text}"
