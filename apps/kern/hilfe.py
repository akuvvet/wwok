"""Hilfe der Schnellbedienung (Strg+K): Fragen wie „Wie buche ich eine Kundenrechnung?“ mit kurzen Wegen durch die Oberfläche.

Jeder Eintrag hat eine Frage, Stichworte (Synonyme, Umgangssprache, Tippvarianten), einen oder mehrere Wege (Schrittfolge
„A → B → C“, optional mit Hinweis) und ein Sprungziel (`url_name`, optional `args` und `query`).
Die Wege folgen der echten Oberfläche (Seitenleiste, Belegmaske, Zahlungsseite …) und den Fachregeln aus README Abschnitt 18;
ändert sich ein Knopf oder eine Regel, hier nachziehen. Der Test `test_hilfe.py` prüft, dass jedes Sprungziel noch auflösbar ist.

Suche: `hilfe_suchen(text)` bewertet Wörter in Frage und Stichworten (Groß/Klein, Umlaute als ae/oe/ue, Stoppwörter ignoriert).
Keine Daten aus der Datenbank, keine Rechteprüfung nötig: es sind feste Texte ohne Preise, Namen oder Bankdaten."""
import re
from dataclasses import dataclass
from functools import lru_cache

from django.urls import reverse

MAX_HILFE_TREFFER = 5
MIN_PUNKTE = 2

# Wörter, die nichts über das Thema sagen (nach der Umlaut-Umschrift)
STOPPWOERTER = frozenset(
    "ich mich mir wir man du sie er es ein eine einen einem einer eines der die das den dem des und oder auch noch nur "
    "wie wo was wann wer wieso weshalb warum wozu wohin woher womit welche welcher welches kann koennen muss soll darf "
    "ist sind wird werde habe haben hat bin zu zur zum im in an am auf aus bei von vom fuer mit nach um ueber bitte "
    "gibt gebe geht gehen mache machen moechte will".split())
FRAGE_ANFAENGE = frozenset("wie wo was wann wer wieso weshalb warum wozu wohin woher womit welche welcher welches kann koennen muss darf gibt".split())
_UMSCHRIFT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


@dataclass(frozen=True)
class Weg:
    titel: str
    schritte: tuple
    hinweis: str = ""


@dataclass(frozen=True)
class Hilfe:
    kennung: str
    frage: str
    stichworte: tuple
    wege: tuple
    url_name: str
    ziel: str  # kurze Beschreibung des Sprungziels, z. B. „Verkauf → Kunde“
    args: tuple = ()
    query: str = ""
    hinweis: str = ""

    def url(self) -> str:
        return reverse(self.url_name, args=self.args) + (f"?{self.query}" if self.query else "")


def _weg(titel, schritte, hinweis=""):
    """Schrittfolge als Text „A → B → C“ angeben; sie wird in die einzelnen Schritte zerlegt."""
    return Weg(titel, tuple(s.strip() for s in schritte.split(" → ")), hinweis)


def _h(kennung, frage, stichworte, wege, url_name, ziel, hinweis="", args=(), query=""):
    return Hilfe(kennung, frage, tuple(s.strip() for s in stichworte.split(",") if s.strip()), tuple(wege), url_name, ziel, tuple(args), query, hinweis)


HILFE = (
    # ---- Verkauf ------------------------------------------------------------------------------------------------
    _h("angebot", "Wie erstelle ich ein Angebot?",
       "angebot schreiben, angebot anlegen, neues angebot, kostenvoranschlag, offerte, angebot an kunde, angebot erstellen",
       [_weg("Über den Kunden", "Verkauf → Kunde → Kunde suchen und wählen → + Angebot → Artikel (Nr. oder Suchwörter) und Menge erfassen, Enter → Speichern: abschließen & Nummer vergeben",
             "Bis zum Speichern ist der Beleg vorläufig und hat keine Nummer; wird der Tab geschlossen, wird er verworfen. Preislistenartikel (Typ P) gibt es nur im Angebot."),
        _weg("Als Kopie eines vorhandenen Belegs", "Verkauf → Alle Belege → Beleg öffnen → Mehr → Kopieren als … → Angebot → Speichern: abschließen & Nummer vergeben")],
       "verkauf:start", "Verkauf → Kunde"),
    _h("bestellung", "Wie erfasse ich eine Kundenbestellung?",
       "auftrag, auftragsbestätigung, bestellung anlegen, kunde hat bestellt, neue bestellung, bestellung erfassen, bestellung erstellen",
       [_weg("Neue Bestellung", "Verkauf → Kunde → Kunde wählen → + Bestellung → Positionen erfassen → Speichern: abschließen & Nummer vergeben",
             "Jede Position mit Lagerartikel hat einen Haken R (reservieren), Standard gesetzt. Typ-P-Positionen wandern beim Speichern mit Warnung in ein neues Angebot desselben Kunden."),
        _weg("Aus einem Angebot", "Angebot öffnen → Mehr → Als Bestellung übernehmen → Speichern: abschließen & Nummer vergeben")],
       "verkauf:start", "Verkauf → Kunde"),
    _h("angebot_zu_bestellung", "Wie mache ich aus einem Angebot eine Bestellung?",
       "angebot übernehmen, angebot umwandeln, angebot annehmen, kunde nimmt angebot an, angebot in auftrag, angebot bestellung",
       [_weg("Umwandeln (empfohlen)", "Verkauf → Kunde → Kunde wählen → Angebot öffnen → Mehr → Als Bestellung übernehmen → Speichern: abschließen & Nummer vergeben",
             "Das Angebot wird umgewandelt: derselbe Beleg, neue Bestellnummer, die alte Nummer bleibt im Verlauf und wird bei der Suche gefunden. Aus einem noch nicht gespeicherten Angebot wird ohne Rückfrage übernommen."),
        _weg("Angebot behalten", "Angebot öffnen → Mehr → Kopieren als … → Bestellung → Speichern: abschließen & Nummer vergeben",
             "Das ist eine echte Kopie; beim Kopieren fragt das Programm, ob das Angebot danach abgeschlossen werden soll.")],
       "verkauf:start", "Verkauf → Kunde"),
    _h("lieferschein", "Wie erstelle ich einen Lieferschein?",
       "lieferschein schreiben, ware liefern, ware ausliefern, lieferschein anlegen, ls, auslieferung, lieferschein erstellen",
       [_weg("Aus der Bestellung", "Verkauf → Kunde → Kunde wählen → Bestellung öffnen → Mehr → Als Lieferschein übernehmen → Speichern: abschließen & Nummer vergeben",
             "Der Lieferschein bucht das Lager ab und lässt sich nur abschließen, wenn der Bestand reicht (für andere reservierte Ware zählt als nicht vorhanden). Reicht er nur für einen Teil, fragt das Programm nach einer Teillieferung. Die übernommene Menge wandert aus der Bestellung in den Lieferschein."),
        _weg("Als Kopie", "Bestellung öffnen → Mehr → Kopieren als … → Lieferschein")],
       "verkauf:start", "Verkauf → Kunde"),
    _h("rechnung", "Wie buche ich eine Kundenrechnung?",
       "rechnung erstellen, rechnung schreiben, rechnung buchen, kundenrechnung, ausgangsrechnung, rechnung anlegen, faktura, neue rechnung, rechnung stellen, rechnung",
       [_weg("Direkt aus der Bestellung", "Verkauf → Kunde → Kunde wählen → Bestellung öffnen → Mehr → Als Rechnung übernehmen → Speichern: abschließen & Nummer vergeben",
             "Geht die ganze Bestellung in die Rechnung, wird sie umgewandelt („vormals AB …“); Zahlungen der Bestellung hängen danach an der Rechnung. Bei Teilmenge entsteht ein neuer Beleg, die Bestellung behält den Rest."),
        _weg("Nach dem Lieferschein", "Verkauf → Kunde → Kunde wählen → abgeschlossenen Lieferschein öffnen → Mehr → Als Rechnung übernehmen → Speichern: abschließen & Nummer vergeben",
             "Eine Rechnung aus dem Lieferschein geht erst nach dessen Abschluss (er bucht das Lager ab). Mehrere Lieferscheine: siehe Sammelrechnung."),
        _weg("Aus einem Angebot oder einer früheren Rechnung", "Beleg öffnen → Mehr → Kopieren als … → Rechnung → Positionen prüfen → Speichern: abschließen & Nummer vergeben")],
       "verkauf:start", "Verkauf → Kunde",
       "Rechnung und Gutschrift sind nach dem Abschluss gesperrt, Korrektur nur per Gutschrift. Ein Rechnungsdatum in der Zukunft gibt es nicht; ein älteres Datum wird beim Abschluss auf das Abschlussdatum gesetzt. Die Rechnung lässt sich nur mit ausreichendem Bestand abschließen und drucken."),
    _h("sammelrechnung", "Wie erstelle ich eine Sammelrechnung aus mehreren Lieferscheinen?",
       "sammelrechnung, lieferscheine abrechnen, mehrere lieferscheine eine rechnung, lieferscheine zusammen, lieferscheine in rechnung",
       [_weg("Lieferscheine ankreuzen", "Verkauf → Kunde → Kunde wählen → in der Belegliste die Lieferscheine ankreuzen → Ausgewählte Lieferscheine in Rechnung umwandeln → Speichern: abschließen & Nummer vergeben",
             "Der Knopf erscheint erst bei mindestens zwei offenen Lieferscheinen; das Lieferscheindatum steht bei der Position. Nur Lieferscheine mit gleichem Belegrabatt lassen sich zusammenfassen.")],
       "verkauf:start", "Verkauf → Kunde"),
    _h("teillieferung", "Wie liefere ich eine Bestellung teilweise (Teillieferung)?",
       "teillieferung, teilweise liefern, nachlieferung, lager reicht nicht, bestand reicht nicht, nicht alles lieferbar, restmenge, rest nachliefern",
       [_weg("Bestand reicht nicht für alles", "Lieferschein oder Rechnung öffnen → Speichern: abschließen & Nummer vergeben → Rückfrage „Soll eine Teillieferung erfolgen?“ → Ja, Teillieferung abschließen",
             "Die lieferbaren Positionen werden als eigener Beleg abgeschlossen, der Rest bleibt unverändert als Entwurf. Versandkosten laufen mit der Teillieferung. Bei nur einer Position gibt es keine Teillieferung."),
        _weg("Menge selbst bestimmen", "Bestellung öffnen → Mehr → Als Lieferschein übernehmen → im Lieferschein bei der Position bearbeiten und die Menge senken → Speichern: abschließen & Nummer vergeben",
             "Die übernommene Menge wandert aus der Bestellung in den Lieferschein, die Bestellung behält nur den Rest. Senkt man die Menge im Entwurf, geht die Differenz zurück in die Bestellung.")],
       "verkauf:start", "Verkauf → Kunde"),
    _h("gutschrift_zur_rechnung", "Wie erstelle ich eine Gutschrift (Storno) zu einer Rechnung?",
       "gutschrift, storno, rechnung stornieren, rechnung korrigieren, retoure, rechnung falsch, erstattung, kunde schickt zurück, gutschrift zur rechnung, stornorechnung",
       [_weg("Aus der abgeschlossenen Rechnung", "Verkauf → Kunde → Kunde wählen → Rechnung öffnen → Mehr → Storno / Gutschrift → bei Lagerartikeln wählen: Reparaturbestand, Lagerbestand oder keine Ware zurück → Positionen prüfen → Speichern: abschließen & Nummer vergeben",
             "Spalte B zeigt je Lagerartikel R, B oder – (Klick wechselt); gebucht wird beim Abschließen, einmal. Über alle Gutschriften einer Rechnung darf höchstens die Rechnungsmenge zurückgebucht werden. Liegt die Summe der Gutschriften über dem Rechnungsbetrag, verlangt das Programm eine Begründung.")],
       "verkauf:start", "Verkauf → Kunde",
       "Die Gutschrift mindert die Rechnung nicht von selbst: Sie wird danach verrechnet oder ausgezahlt (siehe „Gutschrift verrechnen“). Gutschriften entstehen nicht per Kopieren."),
    _h("freie_gutschrift", "Wie erstelle ich eine Gutschrift ohne Rechnung?",
       "freie gutschrift, kulanz, preisnachlass, gutschrift ohne rechnung, kulanzgutschrift, nachlass, gutschrift frei",
       [_weg("Unter der Kundenadresse", "Verkauf → Kunde → Kunde wählen → + Gutschrift (unter der Adresse) → Positionen erfassen → Speichern: abschließen & Nummer vergeben → Grund der Gutschrift eintragen → Mit Grund abschließen",
             "Der Grund ist Pflicht und wird gedruckt; unter „Bezugnahme“ lässt sich frei ein Bezug eintragen. Spalte B steht standardmäßig auf „keine Ware zurück“ (R oder B bucht Reparatur- bzw. Lagerbestand).")],
       "verkauf:start", "Verkauf → Kunde",
       "Nach dem Abschluss ist sie Guthaben des Kunden in den Offenen Posten und wird wie jede Gutschrift verrechnet oder ausgezahlt."),
    _h("proforma", "Wie erstelle ich eine Proforma-Rechnung (Vorkasse)?",
       "proforma, vorkasse, vorauskasse, anzahlung, vorausrechnung, vorab bezahlen, proformarechnung",
       [_weg("Aus der Bestellung", "Bestellung öffnen → Mehr → Kopieren als … → Proforma-Rechnung → Speichern: abschließen & Nummer vergeben → Zahlung buchen (Symbol Zahlungen oder Bankabgleich) → nach voller Zahlung: Mehr → In Rechnung umwandeln",
             "Eine Proforma entsteht nur aus einer Bestellung und hat immer die Zahlungsbedingung Vorkasse. Sie nimmt die Menge aus der Bestellung. Teilzahlungen und Überzahlung (Guthaben) sind möglich; mit der ersten Zahlung sind Positionen und Beträge fest. „In Rechnung umwandeln“ geht erst, wenn sie vollständig bezahlt ist."),
        _weg("Kunde kauft nicht", "Zahlungsseite der Proforma → gezahlten Betrag als negative Zahlung zurückbuchen → Proforma öffnen → Mehr → Stornogrund eintragen → Proforma stornieren",
             "Es entsteht ein eigener Storno-Beleg, die Menge steht danach wieder in der Bestellung.")],
       "verkauf:start", "Verkauf → Kunde",
       "Eine Proforma ist keine Forderung und kein Umsatz: Sie steht nicht in der Summe der Offenen Posten (eigener Abschnitt „Offene Proformas“), nicht im Mahnwesen und nicht in der DATEV-Belegliste."),
    _h("abo", "Wie richte ich eine wiederkehrende Bestellung (Abo) ein?",
       "abo, abonnement, wiederkehrend, wiederkehrende bestellung, wiederkehrende rechnung, dauerauftrag, monatliche rechnung, stichtag, regelmäßig berechnen, serienrechnung, turnus",
       [_weg("Aus einer Rechnung", "Verkauf → Kunde → Kunde wählen → Rechnung öffnen → Mehr → Bezeichnung, Rhythmus und Datum der nächsten Bestellung eintragen → Wiederkehrend einrichten",
             "Rhythmus: monatlich, vierteljährlich, halbjährlich, jährlich oder an Stichtagen (Tage frei wählbar, z. B. „10, 25“ oder „1, 15, letzter“). Nicht möglich bei Rechnungen mit Preislistenartikeln oder Gruppen mit Lagerartikeln."),
        _weg("Verwalten", "Verkauf → Wiederkehrende Bestellungen → Bestellung bearbeiten, Pausieren, Aktivieren oder Löschen",
             "Preise und Mengenregeln je Position (feste Menge, Wochentage, Wochenende und Feiertage, alle Kalendertage) pflegt man in der Verwaltung.")],
       "verkauf:abos", "Verkauf → Wiederkehrende Bestellungen",
       "Zu jedem Stichtag entsteht eine Bestellung (mit Nummer) mit dem Tag der Erstellung als Belegdatum; daraus entsteht von Hand die Rechnung. Ein Belegdatum in der Zukunft gibt es nicht."),
    _h("beleg_senden", "Wie verschicke oder drucke ich einen Beleg (PDF, E-Mail, Druck)?",
       "pdf, drucken, mail, email, e-mail, beleg senden, rechnung versenden, rechnung mailen, ausdrucken, beleg verschicken, beleg per mail",
       [_weg("Aus der Belegmaske", "Beleg öffnen → Symbol PDF, Drucken oder E-Mail → bei E-Mail: Empfänger, Betreff und Text prüfen → Senden",
             "Nach dem Speichern, PDF oder E-Mail kehrt die Anwendung zur Belegübersicht des Kunden zurück.")],
       "verkauf:belege", "Verkauf → Alle Belege",
       "Vor „Speichern: abschließen & Nummer vergeben“ gibt es nichts nach außen: kein PDF, kein Druck, keine Mail. Lieferschein und Rechnung sind außerdem gesperrt, solange der Bestand nicht reicht."),
    _h("beleg_ohne_nummer", "Warum hat mein Beleg keine Nummer („nicht gespeichert“)?",
       "nicht gespeichert, keine nummer, beleg verschwunden, verworfen, vorläufig, nummer vergeben, beleg speichern, beleg abschließen, abschließen",
       [_weg("Beleg abschließen", "Beleg öffnen → Speichern: abschließen & Nummer vergeben (Diskettensymbol)",
             "Neue Belege (Neuer Beleg, Übernehmen, Kopieren als, Gutschrift, Sammelrechnung) sind vorläufig und haben keine Nummer, damit die Nummernkreise lückenlos bleiben. Beim Verlassen fragt ein Fenster: Abschließen & Nummer vergeben, Verwerfen oder Zurück zum Beleg. Wird der Tab geschlossen, wird der Beleg nach etwa einer Minute verworfen."),
        _weg("Nicht gespeicherte Belege finden", "Verkauf → Kunde → Startseite → Bereich „Nicht gespeicherte Belege“ → Beleg öffnen")],
       "verkauf:start", "Verkauf → Kunde"),
    _h("beleg_verwerfen", "Wie lösche oder verwerfe ich einen Beleg?",
       "beleg löschen, verwerfen, rechnung löschen, angebot löschen, abbrechen, entwurf löschen, bestellung löschen",
       [_weg("Im Beleg", "Beleg öffnen → Mehr → Verwerfen (nicht gespeicherter Beleg) oder „<Belegart> löschen“",
             "Verwerfen nimmt alles zurück: Mengen, Seriennummern, Versandkosten und den Zustand der Bestellung. Löschen geht bei Angebot, Bestellung und noch nicht nummerierten Entwürfen, sofern keine Folgebelege, Mails, Zahlungen oder Lagerbuchungen daran hängen. Lieferscheine und abgeschlossene Rechnungen lassen sich nicht löschen: Korrektur per Gutschrift.")],
       "verkauf:belege", "Verkauf → Alle Belege"),
    _h("beleg_suchen", "Wie finde ich einen Beleg?",
       "beleg suchen, rechnung finden, belegnummer, alle belege, rechnung suchen, lieferschein suchen, angebot suchen, belegliste",
       [_weg("Schnellbedienung", "Strg+K → Belegnummer oder Kundenname tippen → Enter"),
        _weg("Belegliste", "Verkauf → Alle Belege → Beleg-Nr., Kunde, Referenz oder Artikel eintragen → Belegart, Status, von/bis wählen → Filtern",
             "Auch die alten Nummern umgewandelter Belege (Angebot, Bestellung) werden gefunden. "
             "Artikel zählt doppelt: die Bezeichnung, wie sie im Beleg steht, und Nummer wie Bezeichnung des Stammartikels."),
        _weg("Positionen ansehen, ohne den Beleg zu öffnen", "Verkauf → Alle Belege → Zeile anklicken",
             "Unter der Liste stehen die Positionen der markierten Zeile: Artikelnummer, Bezeichnung, Menge, Preis, Rabatt, Netto. "
             "Mit ↑ und ↓ geht es weiter zur nächsten Zeile. Geändert wird eine Position im Beleg selbst (Beleg öffnen).")],
       "verkauf:belege", "Verkauf → Alle Belege"),
    _h("positionen_import", "Wie importiere ich Positionen aus einer Datei?",
       "positionen importieren, excel import, csv, xlsx, stückliste einlesen, positionen aus datei, import beleg",
       [_weg("Im Beleg", "Angebot, Bestellung, Rechnung oder Proforma öffnen → Positionen aus Datei importieren → Datei wählen → Importieren → Speichern: abschließen & Nummer vergeben",
             "CSV, XLS oder XLSX bis 5 MB, höchstens 1000 Zeilen. Pflicht sind die Überschriften nummer und menge; dazu optional benennung, netto, gesamtnetto, Zusatz_pos, Kennzeichen, Rabatt. Alles oder nichts: bei einem Fehler wird nichts übernommen.")],
       "verkauf:start", "Verkauf → Kunde"),
    _h("stammdaten_import", "Wie importiere ich Artikel, Leistungen, Kategorien oder Hersteller?",
       "artikel importieren, leistungsartikel importieren, kategorien importieren, leistungskategorien importieren, hersteller importieren, stammdaten import, excel, csv, beispieldatei, vorlage import",
       [_weg("In der Verwaltung", "Verwaltung → Artikel, Leistungsartikel, Kategorien, Leistungskategorien oder Hersteller → Importieren → Beispieldatei (Excel oder CSV) herunterladen und ausfüllen → Datei wählen → Importieren",
             "Spalten werden an der Überschrift erkannt. Vorhandene Einträge werden aktualisiert, leere Zellen lassen alte Werte stehen. Bei einem Fehler wird nichts importiert. Den Bestand bucht die Inventur.")],
       "admin:stammdaten_artikel_import", "Verwaltung → Artikel → Importieren"),
    _h("versand", "Wie erzeuge ich ein Versandlabel (DHL oder Deutsche Post)?",
       "versand, paketlabel, dhl, label, paket, brief, aufkleber, deutsche post, briefmarke, sendungsnummer, paketaufkleber",
       [_weg("Aus dem Beleg", "Abgeschlossenen Lieferschein oder Rechnung öffnen → Symbol Versand → DHL-Label automatisch erzeugen (Paket) oder Briefmarke kaufen & Etikett drucken (Brief)",
             "Alternativ die Sendungsnummer von Hand eintragen und Versand speichern. Versendungen stehen unten mit Sendungsverfolgung, Versand-Mail und Erstatten.")],
       "verkauf:belege", "Verkauf → Alle Belege"),
    _h("xrechnung", "Wie erzeuge ich eine XRechnung (E-Rechnung) für einen Kunden?",
       "xrechnung, e-rechnung, erechnung, zugferd, leitweg, behörde, elektronische rechnung, xml rechnung",
       [_weg("Aus der Rechnung", "Abgeschlossene Rechnung oder Gutschrift öffnen → Mehr → XRechnung (XML) herunterladen",
             "Die Leitweg-ID des Kunden steht in der Kundenmaske im Reiter Steuer & Bank.")],
       "verkauf:belege", "Verkauf → Alle Belege"),
    _h("wiedervorlage", "Wie setze ich eine Wiedervorlage für einen Kunden?",
       "wiedervorlage, erinnerung, termin kunde, notiz kunde, nachfassen, heute zu erledigen, aufgabe, follow up",
       [_weg("Auf der Kundenseite", "Verkauf → Kunde → Kunde wählen → Wiedervorlage: fällig am und Notiz eintragen → Merken",
             "Fällige Wiedervorlagen stehen auf der Startseite Verkauf → Kunde unter „Heute zu erledigen“ und werden dort mit „erledigt“ abgehakt.")],
       "verkauf:start", "Verkauf → Kunde"),

    # ---- Finanzen -----------------------------------------------------------------------------------------------
    _h("zahlung_kunde", "Wie buche ich einen Zahlungseingang?",
       "zahlung eingang, zahlung buchen, rechnung bezahlt, bezahlt markieren, kunde hat bezahlt, zahlung verbuchen, geld eingegangen, überweisung, bar bezahlt, kundenzahlung, zahlungseingang, als bezahlt",
       [_weg("Über Offene Posten", "Finanzen → Offene Posten → Kunden → Rechnung anklicken (Zahlungsseite) → Zahlung buchen: Datum, Betrag brutto, Zahlart → Buchen"),
        _weg("Aus dem Beleg", "Rechnung öffnen → Symbol Zahlungen → Zahlung buchen → Buchen"),
        _weg("Automatisch per Kontoauszug", "Finanzen → Zahlungen → Kunden → Kontoauszug (CSV oder CAMT.053-XML) → Importieren",
             "Eingänge mit Rechnungsnummer im Verwendungszweck werden beim Import gebucht, solange sie die offenen Beträge nicht übersteigen.")],
       "finanzen:offene_posten", "Finanzen → Offene Posten → Kunden",
       "Zahlungen sind unveränderlich; eine Korrektur ist nur eine Stornobuchung mit Grund."),
    _h("teilzahlung", "Wie buche ich eine Teilzahlung oder Anzahlung?",
       "teilzahlung, anzahlung buchen, ratenzahlung, teilbetrag, teilweise bezahlt, restbetrag, teilbezahlt",
       [_weg("Teilzahlung auf eine Rechnung", "Finanzen → Offene Posten → Kunden → Rechnung anklicken → bei Betrag brutto den gezahlten Teilbetrag eintragen → Skonto auf 0,00 setzen → Buchen",
             "Bei einer Teilzahlung steht der Rest im Feld Skonto: so lassen = dem Kunden als Skonto gewährt, die Rechnung ist ausgeglichen; auf 0 setzen = der Rest bleibt offen."),
        _weg("Anzahlung auf eine Bestellung", "Bestellung öffnen (mit Nummer) → Symbol Zahlungen → Zahlung buchen → Buchen",
             "Wird die ganze Bestellung in die Rechnung umgewandelt, hängen die Zahlungen danach an der Rechnung und sind dort Teilzahlungen.")],
       "finanzen:offene_posten", "Finanzen → Offene Posten → Kunden"),
    _h("skonto", "Wie gewähre ich Skonto bei einer Kundenzahlung?",
       "skonto abziehen, skonto buchen, abzug 2 prozent, skontoabzug, zahlungsabzug, skonto gewähren, skonto kunde",
       [_weg("Auf der Zahlungsseite", "Finanzen → Offene Posten → Kunden → Rechnung anklicken → Betrag brutto = tatsächlich überwiesener Betrag → Feld „Skonto (gewährter Rest)“ so lassen → Buchen",
             "Die Zahlungsseite nennt Skontofrist und höchsten Skontobetrag laut Zahlungsbedingung der Rechnung. Der Rest wird als Skonto gebucht und die Rechnung ist ausgeglichen. Soll der Rest offen bleiben, das Feld auf 0 setzen.")],
       "finanzen:offene_posten", "Finanzen → Offene Posten → Kunden"),
    _h("gutschrift_verrechnen", "Wie verrechne ich eine Gutschrift mit einer Rechnung?",
       "gutschrift verrechnen, gutschrift anrechnen, gutschrift abziehen, gutschrift offene posten, verrechnung, gutschrift abgleichen",
       [_weg("Von der Rechnung aus", "Finanzen → Offene Posten → Kunden → Rechnung des Kunden anklicken → Gutschriften verrechnen → Betrag prüfen → Verrechnen"),
        _weg("Von der Gutschrift aus", "Finanzen → Offene Posten → Kunden → Gutschrift anklicken → Mit Rechnung verrechnen → bei der Rechnung Betrag prüfen → Verrechnen")],
       "finanzen:offene_posten", "Finanzen → Offene Posten → Kunden",
       "Eine Gutschrift mindert die Rechnung nicht von selbst, denn der Kunde kann schon bezahlt haben: Hat er bezahlt, zuerst die Zahlung buchen. Der Betrag ist frei wählbar bis zum Rest der Gutschrift; ein Rest bleibt als Guthaben. Verrechnungen sind unveränderlich, Admins heben sie per Gegenbuchung mit Pflichtgrund auf."),
    _h("gutschrift_auszahlen", "Wie zahle ich eine Gutschrift oder ein Guthaben an den Kunden aus?",
       "gutschrift auszahlen, erstattung, rückzahlung, geld zurück, rückerstattung, auszahlung, kunde bekommt geld zurück, negative zahlung, überweisung an kunden",
       [_weg("Gutschrift auszahlen", "Finanzen → Offene Posten → Kunden → Gutschrift anklicken → Auszahlung an den Kunden: Datum, Betrag, Zahlart → Auszahlen",
             "Höchstens der noch freie Betrag; bar nur bei ausreichendem Kassenstand. Das geht auch ohne Rechnung des Kunden. Storno per Gegenbuchung mit Grund (nur Admin)."),
        _weg("Überzahlung einer Rechnung zurückzahlen", "Zahlungsseite der Rechnung → Zahlung buchen mit negativem Betrag (Gegenbuchung) → Buchen",
             "Verrechnetes Guthaben lässt sich erst nach dem Aufheben der Verrechnung zurückzahlen.")],
       "finanzen:offene_posten", "Finanzen → Offene Posten → Kunden"),
    _h("ueberzahlung", "Wie verrechne ich ein Guthaben oder eine Überzahlung des Kunden?",
       "überzahlung, guthaben, zu viel bezahlt, guthaben verrechnen, vorkasse verrechnen, anzahlung verrechnen, guthaben kunde",
       [_weg("Auf einer offenen Rechnung", "Finanzen → Offene Posten → Kunden → offene Rechnung desselben Kunden anklicken → Guthaben des Kunden verrechnen → Betrag prüfen → Verrechnen",
             "Die Zahlungsseite schlägt vorhandene Guthaben vor (höchstens der offene Betrag); nichts geschieht automatisch. Die Verrechnung steht auf beiden Belegen und im Kontoauszug, Korrektur nur per Aufheben mit Grund.")],
       "finanzen:offene_posten", "Finanzen → Offene Posten → Kunden",
       "Im Bankabgleich bleibt eine Überzahlung ein Vorschlag, den man von Hand bucht; der Rest bleibt als Guthaben auf der letzten genannten Rechnung."),
    _h("zahlung_storno", "Wie storniere ich eine falsch gebuchte Zahlung?",
       "zahlung stornieren, zahlung löschen, falsch gebucht, buchung rückgängig, zahlung korrigieren, zahlung falsch",
       [_weg("Auf der Zahlungsseite", "Finanzen → Offene Posten → Kunden → Rechnung anklicken → Stornogrund (Pflicht) bei der Zahlung eintragen → Stornieren",
             "Nur für Admins. Zahlungen sind unveränderlich; die Korrektur steht als eigene Stornozeile mit Grund. Ein Bankumsatz wird erst frei, wenn keine seiner Zahlungen mehr gilt.")],
       "finanzen:offene_posten", "Finanzen → Offene Posten → Kunden"),
    _h("mahnung", "Wie erstelle ich eine Mahnung?",
       "mahnung, mahnen, mahnwesen, zahlungserinnerung, überfällig, rechnung nicht bezahlt, säumig, mahnstufe, mahngebühr, erinnerung, mahnungen",
       [_weg("Mahnungen erstellen", "Finanzen → Weiteres → Mahnwesen → Fällige Mahnungen: Rechnungen abhaken → Ausgewählte Mahnungen erstellen",
             "Vorgeschlagen wird die nächste Mahnstufe, sobald die Fälligkeit laut Zahlungsbedingung überschritten ist. Proformas werden nicht gemahnt."),
        _weg("Mahnungen versenden", "Finanzen → Weiteres → Mahnwesen → Bisherige Mahnungen → PDF oder E-Mail (mehrere: Ausgewählte drucken oder Ausgewählte senden)"),
        _weg("Mahnstufen und Gebühren ändern", "Verwaltung (Zahnrad oben) → Mahnstufen → Stufe öffnen → Tage, Gebühr und Text ändern → Speichern")],
       "finanzen:mahnwesen", "Finanzen → Weiteres → Mahnwesen",
       "Überfällige Rechnungen stehen in den Offenen Posten rot; der Filter „Überfällige“ zeigt nur diese."),
    _h("bankabgleich", "Wie gleiche ich den Kontoauszug mit der Bank ab?",
       "bankabgleich, kontoauszug import, camt, csv bank, bankumsätze, überweisungen zuordnen, zahlungen importieren, bank import, kontoauszug einlesen",
       [_weg("Kontoauszug importieren", "Finanzen → Zahlungen → Kunden → Kontoauszug (CSV oder CAMT.053-XML) wählen → Importieren → Liste „Offen“ prüfen → Buchen oder Ignorieren",
             "Eingänge mit Rechnungs- oder Proformanummer im Verwendungszweck werden beim Import gebucht, solange sie die offenen Beträge nicht übersteigen: genau passend, als Teilzahlung oder bei mehreren Nummern der Reihe nach. Eine Überzahlung bleibt Vorschlag und wird von Hand gebucht."),
        _weg("Von Hand zuordnen", "Finanzen → Zahlungen → Kunden → Umsatz in „Offen“ → Rechnungsnummer eintragen (z. B. RE2026-00001) → Buchen"),
        _weg("Vorschläge nachbuchen", "Finanzen → Zahlungen → Kunden → Vorschläge nachbuchen")],
       "finanzen:bank", "Finanzen → Zahlungen → Kunden"),
    _h("lastschrift", "Wie ziehe ich Rechnungen per Lastschrift ein (SEPA-Datei)?",
       "lastschrift, sepa einzug, einzug, pain.008, sepa-datei kunden, abbuchen, lastschriftdatei, sepa lastschrift",
       [_weg("SEPA-Datei erzeugen", "Finanzen → Zahlungen → Kunden → Lastschrift-Einzug (SEPA-Datei) → Einzugsdatum (mindestens morgen) wählen → Lastschriftdatei (pain.008) erzeugen"),
        _weg("Einzug aufheben", "Finanzen → Zahlungen → Kunden → Lastschrift-Einzug → Bisherige Einzüge → Grund eintragen → Aufheben",
             "Nur für Admins; die Rechnungen sind danach wieder offen und lassen sich erneut einziehen oder mahnen.")],
       "finanzen:lastschrift", "Finanzen → Zahlungen → Kunden → Lastschrift"),
    _h("kassenbuch", "Wie führe ich das Kassenbuch (Bareinnahmen und -ausgaben)?",
       "kasse, kassenbuch, bargeld, barzahlung, einlage, entnahme, kassenstand, kassenbuchung",
       [_weg("Buchung eintragen", "Finanzen → Weiteres → Kassenbuch → Datum, Art, Betrag und Buchungstext eintragen → Buchen",
             "Eine Barauszahlung (etwa einer Gutschrift) ist nur bei ausreichendem Kassenstand möglich.")],
       "finanzen:kassenbuch", "Finanzen → Weiteres → Kassenbuch"),
    _h("datev", "Wie erstelle ich den DATEV-Export für den Steuerberater?",
       "datev, steuerberater, buchungsstapel, export buchhaltung, buchhaltung, export steuerberater, monatsabschluss, datev export",
       [_weg("Export", "Finanzen → Weiteres → DATEV-Export → Zeitraum von / bis eintragen → Buchungsstapel herunterladen",
             "Proformas sind keine Belegzeilen im Export. Die USt auf Anzahlungen und das DATEV-Konto für erhaltene Anzahlungen klärt der Steuerberater; bis dahin exportiert DATEV nur die Zahlung.")],
       "finanzen:datev", "Finanzen → Weiteres → DATEV-Export"),
    _h("offene_posten", "Wie sehe ich offene Posten und den Kontoauszug eines Kunden?",
       "offene posten, op liste, kontoauszug kunde, saldo, wer schuldet, forderungen, überfällige rechnungen, kundenkonto, kontoauszug",
       [_weg("Offene Posten", "Finanzen → Offene Posten → Kunden → Kunde oder Belegnummer suchen → Suchen (Filter: Alle, Überfällige) → Kontoauszug"),
        _weg("Von der Kundenseite", "Verkauf → Kunde → Kunde wählen → Symbol Kontoauszug",
             "Der Saldo rechnet unverrechnete Gutschriften mit. Proformas stehen getrennt als „Offene Proformas“.")],
       "finanzen:offene_posten", "Finanzen → Offene Posten → Kunden"),

    # ---- Einkauf ------------------------------------------------------------------------------------------------
    _h("lieferantenbestellung", "Wie bestelle ich Ware bei einem Lieferanten?",
       "lieferantenbestellung, bestellung beim lieferanten, ware bestellen, einkauf bestellen, nachbestellen, ek bestellung, bestellung lieferant, nachbestellung, lieferant bestellen",
       [_weg("Neue Bestellung", "Einkauf → Bestellungen → Neue Bestellung bei … (Lieferant wählen) → Anlegen → Artikel suchen, Menge, EK netto (leer = Preisliste) → Position übernehmen → Speichern: abschließen & Nummer vergeben → Als bestellt markieren oder Per E-Mail bestellen",
             "Eine neue Bestellung ist vorläufig und hat noch keine Nummer: „Speichern: abschließen & Nummer vergeben“ vergibt sie (ohne Position nicht möglich); beim Verlassen fragt das Programm Abschließen, Verwerfen oder Zurück. Als bestellt gilt eine Menge erst ab Status „bestellt“; Entwürfe und Stornos zählen nicht. Eine Code-Eingabe ergibt eine Preislistenposition (P), keinen Artikel."),
        _weg("Über den Lieferanten", "Einkauf → Lieferant → Lieferant wählen → + Bestellung → Positionen erfassen → Speichern: abschließen & Nummer vergeben → Als bestellt markieren")],
       "einkauf:bestellungen", "Einkauf → Bestellungen"),
    _h("bestellung_aus_beleg", "Wie bestelle ich die Ware einer Kundenbestellung beim Lieferanten?",
       "in lieferantenbestellung kopieren, kundenbestellung bestellen, ware nachbestellen für kunde, fehlende ware bestellen, günstigste aufteilung, streckengeschäft, bestellung aus beleg",
       [_weg("Aus dem Kundenbeleg", "Kundenbestellung oder Angebot öffnen (gespeichert) → Mehr → In Lieferantenbestellung kopieren → bei mehreren Lieferanten: Lieferanten wählen → Lieferantenbestellung anlegen",
             "Je Lieferant entsteht eine Entwurfsbestellung. Ist aus einer Kundenbestellung schon etwas bestellt, fragt das Programm: „Nur Fehlendes bestellen“ oder „Trotzdem komplett neu bestellen“ (Grund Pflicht). Die Lieferantenwahl vergleicht den Warenwert: „Günstigste Aufteilung“ ist vorbelegt, „Alles bei …“ zeigt den Unterschied in Euro.")],
       "einkauf:bestellungen", "Einkauf → Bestellungen",
       "Im Kundenbeleg steht je Position „bestellt: x von y“. Gesperrte Lieferanten fehlen in der Auswahl."),
    _h("bestellvorschlag", "Wie finde ich heraus, was ich nachbestellen muss (Bestellvorschlag, Bedarf)?",
       "bestellvorschlag, bedarf, nachbestellen, mindestbestand, was fehlt, fehlmenge, einkaufsliste, unterbestand, was muss ich bestellen",
       [_weg("Alles auf einmal bestellen", "Einkauf → Bestellvorschlag → je Artikel Menge und Lieferant prüfen (vorgewählt: der günstigste) → Bestellungen anlegen (nicht abgeschlossen)",
             "Der Bedarf ergibt sich aus Mindestbestand, Verkäufen und Kundenbestellungen, abzüglich schon bestellter Mengen; unter „vorhanden“ steht, in welcher Bestellung die Ware schon ist. "
             "Unter der Tabelle steht, welche Bestellungen entstehen (je Lieferant mit Summe)."),
        _weg("Gerade bei einem Lieferanten bestellen", "Lieferantenbestellung öffnen → Tab Bedarf → Artikel abhaken (Kopf-Haken: alle / keine; die Summe wird gezeigt) → Ausgewählte in die Bestellung übernehmen",
             "Der Tab zeigt dieselbe Liste wie der Bestellvorschlag, mit dem EK dieses Lieferanten; „nur Artikel, die … führt“ filtert.")],
       "einkauf:vorschlag", "Einkauf → Bestellvorschlag"),
    _h("wareneingang", "Wie buche ich einen Wareneingang?",
       "wareneingang, ware angekommen, lieferung erhalten, ware einbuchen, einlagern, lieferschein lieferant, scan, ware eingetroffen, paket angekommen, ins lager buchen, ware annehmen",
       [_weg("Neuen Wareneingang starten", "Einkauf → Wareneingänge → Zu Bestellung (oder Lieferant) wählen → Wareneingang starten → Scan / Eingabe (EAN, Artikel-, Hersteller- oder Bestellnr.), Menge → Position übernehmen → Ins Lager buchen",
             "Nebenkosten und den Lieferschein des Lieferanten trägt man im Reiter „Lieferschein & Kosten“ ein. Weicht der Kalkulations-EK um mehr als 50 % ab, fragt das Programm vor dem Buchen."),
        _weg("Aus der Lieferantenbestellung", "Lieferantenbestellung öffnen → Wareneingang erfassen → Reiter Bestellungen: Komplett übernehmen oder Ausgewählte übernehmen → Ins Lager buchen"),
        _weg("Über den Lieferanten", "Einkauf → Lieferant → Lieferant wählen → + Wareneingang")],
       "einkauf:wareneingaenge", "Einkauf → Wareneingänge",
       "Ein eigener Artikel (A) entsteht aus einem Preislistenartikel nie still, sondern nur nach Rückfrage („Als Artikel (A) anlegen und erfassen“ oder „Im Formular anlegen …“). Bei Software-Artikeln werden die ESD-Keys eingetragen, ein Key je Stück."),
    _h("wareneingang_teil", "Was tun, wenn der Lieferant nur einen Teil geliefert hat?",
       "teillieferung lieferant, fehlt, nicht geliefert, rest offen, unvollständig, unterlieferung, doch geliefert, rest nicht lieferbar, lieferant liefert nicht alles",
       [_weg("Im Wareneingang", "Einkauf → Wareneingänge → Wareneingang öffnen → bei der Position Menge verringern oder × „nicht geliefert“ → Ins Lager buchen",
             "Gebucht wird nur die gelieferte Menge; der Rest bleibt in der Lieferantenbestellung offen (Status „teilweise geliefert“) und der nächste Wareneingang übernimmt nur den Rest. „doch geliefert“ nimmt eine entfernte Position wieder auf."),
        _weg("Rest wird nicht mehr geliefert", "Einkauf → Bestellungen → Lieferantenbestellung öffnen → Rest nicht mehr lieferbar")],
       "einkauf:wareneingaenge", "Einkauf → Wareneingänge"),
    _h("lieferantenrechnung", "Wie buche ich eine Lieferantenrechnung?",
       "lieferantenrechnung, eingangsrechnung, rechnung vom lieferanten, eingangsrechnung erfassen, kreditorenrechnung, rechnung erfassen, e-rechnung einlesen, xrechnung lieferant, zugferd lieferant",
       [_weg("Von Hand erfassen", "Einkauf → Lieferantenrechnungen → Lieferant tippen, Rechnungsnr., Datum, Netto, Brutto eintragen → darunter erscheinen die freien Wareneingänge des Lieferanten: gleich anhaken (bei Teillieferungen mehrere; Netto wird aus der Summe vorgeschlagen) → Erfassen. Später geht es auch in der Rechnung unter Verknüpfte Wareneingänge → Verknüpfung speichern",
             "Weicht das Netto von der Summe der verknüpften Wareneingänge um mehr als 1 Cent ab, wird es gemeldet; Zahlung und SEPA-Export bleiben möglich. Klären: Beträge korrigieren („Beträge korrigieren“) oder die Abweichung mit Begründung bestätigen."),
        _weg("E-Rechnung einlesen", "Einkauf → Lieferantenrechnungen → E-Rechnung einlesen (XRechnung/ZUGFeRD: XML oder PDF) → Einlesen"),
        _weg("Falsch erfasste Rechnung entfernen", "Einkauf → Lieferantenrechnungen → Rechnung öffnen → Beleg löschen – Notiz (Pflicht) → Beleg löschen",
             "Nur offene Rechnungen; bezahlte erst nach dem Zahlungsstorno (Admin), SEPA-exportierte nicht.")],
       "einkauf:rechnungen", "Einkauf → Lieferantenrechnungen"),
    _h("lieferantenrechnung_bezahlen", "Wie bezahle ich eine Lieferantenrechnung?",
       "lieferantenrechnung bezahlen, sepa lieferanten, überweisung lieferant, starmoney, rechnung bezahlt lieferant, sepa datei, skonto lieferant, zahlungslauf, lieferant bezahlen",
       [_weg("SEPA-Sammelüberweisung", "Finanzen → Zahlungen → Lieferanten → Rechnungen ankreuzen → Ausführungsdatum eintragen → SEPA-Datei für Auswahl erzeugen (StarMoney) → nach der Ausführung bei der Rechnung Als bezahlt buchen",
             "„SEPA erstellt, nicht gebucht“ zeigt exportierte Rechnungen, die noch nicht als bezahlt gebucht sind."),
        _weg("Einzelne Rechnung", "Einkauf → Lieferantenrechnungen → Rechnung öffnen → bezahlt am und Betrag prüfen → Als bezahlt buchen",
             "Zahlung stornieren geht nur für Admins mit Grund (Pflicht).")],
       "finanzen:zahlungen_lieferanten", "Finanzen → Zahlungen → Lieferanten",
       "Die Spalte „zu zahlen“ zeigt den Betrag mit Skonto; läuft die Skontofrist in 2 Tagen ab, steht ein %. Bei ungeklärter Abweichung zum Wareneingang sind Zahlung und SEPA-Export gesperrt."),
    _h("we_ruecksendung", "Wie sende ich Ware an den Lieferanten zurück (WE-Rücksendung)?",
       "rücksendung, ware zurücksenden, retoure lieferant, falsch geliefert, zu viel geliefert, zurück an lieferant, we rücksendung, rückgabe lieferant, rücksendung lieferant",
       [_weg("Aus dem Wareneingang", "Einkauf → Wareneingänge → Wareneingang öffnen → Ware zurücksenden (WE-Rücksendung) → Positionen und Grund (Pflicht) → Rücksendung anlegen → An Lieferanten versendet → Gutschrift erhalten (Gutschrift netto) oder Ersatz erhalten → Wareneingang",
             "Lehnt der Lieferant ab, kommt die Ware mit „Abgelehnt – zurück ins Lager“ (Grund eintragen) wieder in den Bestand. Der Bestand fällt dabei nie unter 0."),
        _weg("Über den Lieferanten", "Einkauf → Lieferant → Lieferant wählen → Wareneingang wählen → + WE-Rücksendung → Positionen und Grund (Pflicht) → Rücksendung anlegen",
             "Die Rücksendungen des Lieferanten stehen unter Belege (Belegart WE-Rücksendung).")],
       "einkauf:lieferanten", "Einkauf → Lieferant"),
    _h("lieferanten_gutschrift", "Wie erfasse und verrechne ich eine Gutschrift vom Lieferanten?",
       "gutschrift lieferant, lieferantengutschrift, bonus lieferant, preisnachlass lieferant, gutschrift verrechnen lieferant, guthaben lieferant, lieferant zahlt gutschrift, gutschrift ohne rücksendung",
       [_weg("Ohne Rücksendung", "Einkauf → Lieferanten-Gutschriften → Lieferant tippen, Gutschrift-Nr., Datum, Netto, USt, Brutto → Gutschrift erfassen"),
        _weg("Aus einer WE-Rücksendung", "Einkauf → Lieferant → Lieferant wählen → Belege: Rücksendung öffnen →Gutschrift-Nr., Datum, Beträge → Gutschrift erhalten"),
        _weg("Verrechnen oder Zahlung buchen", "Gutschrift öffnen → Mit Rechnung verrechnen (offene Rechnung, Betrag, auch teilweise) oder Zahlung erhalten buchen",
             "Verrechnet sinkt der Zahlbetrag der Rechnung (auch in der SEPA-Datei). Zahlt der Lieferant per Bank, ordnet der Bankabgleich den Eingang zu, wenn die Gutschrift-Nr. im Verwendungszweck steht.")],
       "einkauf:gutschriften", "Einkauf → Lieferanten-Gutschriften"),
    _h("rma", "Wie lege ich einen RMA-Fall (Garantie beim Lieferanten) an?",
       "rma, garantie, defekt, reklamation lieferant, gewährleistung, hersteller einschicken, geräte einschicken, rma nummer, defekte ware lieferant",
       [_weg("Neuer RMA-Fall", "Einkauf → RMA → Lieferant, Artikelnr., Seriennummer, Fehler eintragen → Anlegen → später Status, RMA-Nr. des Lieferanten und Gutschrift eintragen → Speichern"),
        _weg("Aus der Werkstatt", "Werkstatt → Reparaturen → Auftrag öffnen → In Lieferanten-RMA übernehmen"),
        _weg("Über die Seriennummer", "Artikel → Seriennummern → Seriennummer suchen → RMA")],
       "einkauf:rma_liste", "Einkauf → RMA"),
    _h("preisliste_import", "Wie importiere ich eine Lieferanten-Preisliste?",
       "preisliste, preislistenimport, ek import, ek preise, preisliste einlesen, csv xml excel, preise aktualisieren, lieferantenpreise, preislisten",
       [_weg("Neue Preisliste einrichten", "Einkauf → Preislisten & EK-Import → + Neue Preisliste einrichten → Lieferant, Bezeichnung und Datei wählen → Weiter zur Spaltenzuordnung → Zuordnung prüfen → Profil speichern & Vorschau → Jetzt importieren"),
        _weg("Preisliste erneut einlesen", "Einkauf → Preislisten & EK-Import → Preisliste öffnen → Datei wählen → Vorschau anzeigen → Jetzt importieren",
             "Artikel, die in der Preisliste nicht mehr vorkommen, werden entfernt; der Import bricht ab, wenn mehr als 50 % verschwinden würden. Starke Preisänderungen hält das Programm zur Prüfung zurück (Freigeben oder Verwerfen).")],
       "einkauf:preislisten", "Einkauf → Preislisten & EK-Import"),
    _h("lieferantenvergleich", "Wie vergleiche ich Lieferanten und Einkaufspreise?",
       "preisvergleich, lieferantenvergleich, günstigster lieferant, wer ist billiger, ek vergleich, bester preis, günstigster ek",
       [_weg("Produkt vergleichen", "Einkauf → Lieferantenvergleich → Produkt suchen (mehrere Wörter, beliebige Reihenfolge), Menge → Suchen",
             "Der Vergleich rechnet EK mal Menge aus den Preislisten, ohne Versandkosten und Mindestbestellwerte."),
        _weg("Günstigsten EK in die Artikel übernehmen", "Einkauf → Günstigster EK → Jetzt übernehmen",
             "Der Zeitplan dafür steht in der Verwaltung („Zeitplan ändern“).")],
       "einkauf:preisvergleich", "Einkauf → Lieferantenvergleich"),
    _h("lieferant_anlegen", "Wie lege ich einen neuen Lieferanten an?",
       "lieferant anlegen, neuer lieferant, lieferant erfassen, lieferant bearbeiten, lieferant sperren, lieferantenstamm, kurzname",
       [_weg("Neuer Lieferant", "Einkauf → Lieferant → Neuen Lieferanten anlegen (Symbol neben der Suche) → Grunddaten mit Kurzname und Anschrift → Konditionen & Hinweis, Steuer & Bank → Speichern"),
        _weg("Lieferant ändern oder sperren", "Einkauf → Lieferant → Lieferant wählen → Lieferantendaten bearbeiten → Reiter Konditionen & Hinweis → Haken „gesperrt“ → Speichern",
             "Gesperrte Lieferanten fehlen in der Lieferantenwahl für Bestellungen.")],
       "einkauf:lieferanten", "Einkauf → Lieferant",
       "Der Kurzname (z. B. API, Kosatec, Ingram) steht neben Preislistenartikeln. Die Lieferantennummer kommt aus dem Zähler für Lieferanten; eine abgebrochene Maske verbraucht keine Nummer."),

    # ---- Artikel und Lager --------------------------------------------------------------------------------------
    _h("artikel_anlegen", "Wie lege ich einen neuen Artikel an?",
       "artikel anlegen, neuer artikel, artikel erfassen, stammdaten artikel, produkt anlegen, artikelstamm, artikel neu",
       [_weg("Neuer Artikel", "Artikel → Artikel & Bestand → + Neuer Artikel → Reiter Stammdaten (Typ, Bezeichnung, Kategorie) → Kennungen, Preise, Maße & Versand, Online & Texte → Speichern",
             "Der Steuersatz ist mit 19 % vorbelegt. EAN, ASIN/SKU/FNSKU, ISBN, UPC und eBay-ID werden in jeder Artikelsuche gefunden."),
        _weg("Aus einem Preislistenartikel", "Einkauf → Preislistenartikel → Als A-Artikel kopieren → Maske prüfen → Speichern",
             "Ein Artikel A entsteht aus einem Preislistenartikel nie still, sondern mit einer Nummer aus dem Artikelkreis; ein vorhandener Artikel mit gleicher Herstellernummer wird nur verknüpft, wenn auch Hersteller oder EAN passen.")],
       "lager:uebersicht", "Artikel → Artikel & Bestand",
       "Typ A hat Lagerbestand, Typ L ist eine Dienstleistung ohne Bestand (Verwaltung → Leistungsartikel), Typ G eine Gruppe aus Komponenten (Reiter Komponenten), Typ P ein Preislistenartikel eines Lieferanten."),
    _h("artikel_kopieren", "Wie kopiere ich einen Artikel?",
       "artikel duplizieren, artikel als vorlage, ähnlicher artikel, artikel kopie, artikel kopieren",
       [_weg("Als Vorlage", "Artikel → Artikel & Bestand → Artikel anklicken → Kopieren (unten links) → Neuer Artikel öffnet vorausgefüllt → Nummer und Bezeichnung anpassen → Speichern",
             "Angelegt wird erst beim Speichern, die Vorlage bleibt unverändert. Eine mögliche Dublette wird nur gewarnt, nicht gesperrt.")],
       "lager:uebersicht", "Artikel → Artikel & Bestand"),
    _h("preise_aufschlaege", "Wie stelle ich Verkaufspreise und Aufschläge ein?",
       "verkaufspreis, vk preis, aufschlag, marge, preise ändern, vk1 vk2 vk3, preisstufe, kalkulation, preisvorgabe, gruppenpreis, sonderpreis",
       [_weg("Am Artikel", "Artikel → Artikel & Bestand → Artikel anklicken → Reiter Preise → VK1 bis VK3 oder Aufschlag eintragen → Speichern",
             "Ein leeres VK-Feld zeigt den Vorschlag aus EK und Aufschlag (Artikel vor Kategorie vor global). Bleibt er unverändert, wird kein fester Preis gespeichert und der VK folgt dem EK; ein eingetippter Preis ist fest. Der Haken „VK1 für alle VKs übernehmen“ schreibt VK1 fest in VK1 bis VK3."),
        _weg("Für alle Artikel einer Kategorie oder global", "Verwaltung (Zahnrad oben) → Kategorien bzw. Globale Preisvorgabe → Aufschlag VK1 bis VK3 in % eintragen → Speichern"),
        _weg("Für einen Kunden", "Verwaltung (Zahnrad oben) → Kundensonderpreise → Sonderpreis eintragen → Speichern")],
       "lager:uebersicht", "Artikel → Artikel & Bestand",
       "Für Leistungen (Typ L) gelten nur die fest eingetragenen VK, kein Aufschlag auf den EK. Alle Preise werden mit 2 Stellen angezeigt."),
    _h("preis_radar", "Wo verdiene ich zu wenig (Preis-Radar)?",
       "preis radar, marge zu niedrig, verlust, vk senkung prüfen, preisverfall, ek abweichung, preise prüfen, radar",
       [_weg("Liste öffnen", "Dashboard → Kachel Preis-Radar → Liste „Preis-Radar“ → Artikel prüfen → VK übernehmen oder Gesehen",
             "Kategorien: Verlust, Marge unter Ziel, Preisverfall, Preiserhöhung möglich, EK2 prüfen, EK-Abweichung, Kein EK. „Gesehen“ blendet aus, bis sich EK oder VK spürbar ändern. „VK übernehmen“ nur für Admins.")],
       "stammdaten:radar", "Dashboard → Preis-Radar"),
    _h("inventur", "Wie mache ich eine Inventur?",
       "inventur, zählliste, inventur import, inventur excel, inventur csv, zählliste exportieren, bestand zählen, bestandskorrektur, lager zählen, zählen, jahresinventur, differenzen, bestand prüfen",
       [_weg("Sammelinventur", "Artikel → Inventur / Zählliste → Kategorie wählen oder Artikel suchen → Filtern → Zählliste drucken → gezählte Menge eintragen → Differenzen buchen",
             "Ohne Suchbegriff stehen nur Artikel mit Bestand; die Suche findet auch Artikel mit Bestand 0. Leere Zeilen bleiben unverändert. Der Reparaturbestand zählt nicht in die Inventur."),
        _weg("Mit Excel/CSV", "Artikel → Inventur / Zählliste → Export Excel (oder CSV) → Spalte „gezählt“ ausfüllen → Zählung einlesen → Vorschau prüfen → Differenzen buchen",
             "Die Datei braucht die Spalten „Artikel“ und „gezählt“ (auch „Ist“ oder „Menge“). Steht ein Artikel mehrfach in der Datei, werden die Mengen addiert. Gebucht wird erst in der Vorschau."),
        _weg("Einzelner Artikel", "Artikel → Artikel & Bestand → Artikel öffnen → Inventur: gezählter Bestand eintragen → Differenz buchen")],
       "lager:inventur_liste", "Artikel → Inventur / Zählliste"),
    _h("lagerbuchung", "Wie buche ich Lagerbestand von Hand (Zugang, Abgang, Korrektur)?",
       "lagerbuchung, bestand ändern, bestand korrigieren, umbuchen, manuell buchen, zugang, abgang, schwund, bestandsumbuchung, lagerplatz, mindestbestand, bestandshistorie",
       [_weg("Auf der Artikelseite", "Artikel → Artikel & Bestand → Artikel öffnen → Buchen: Grund, Menge, Bemerkung → Buchen",
             "Der Lagerbestand darf nie unter 0 fallen: Abgänge darüber lehnt das Programm ab. Bei „Korrektur“ ist auch eine negative Menge möglich. Lagerplatz und Mindestbestand stehen auf derselben Seite (Speichern), jede Bewegung in der Bestandshistorie.")],
       "lager:uebersicht", "Artikel → Artikel & Bestand"),
    _h("seriennummern", "Wie erfasse und finde ich Seriennummern?",
       "seriennummer, sn, serial, seriennummer suchen, seriennummer erfassen, s/n, gerät nachverfolgen, wieder einlagern, seriennummern",
       [_weg("Im Beleg erfassen", "Beleg öffnen → bei der Position Seriennummern aufklappen → eine Nummer pro Zeile eintragen → speichern",
             "Seriennummern sind auch in nicht gespeicherten Belegen erlaubt und wandern bei einer Teilübernahme mit."),
        _weg("Beim Wareneingang", "Einkauf → Wareneingänge → Wareneingang öffnen → Seriennummern (bei Menge > 1 eine pro Zeile) → Position übernehmen"),
        _weg("Suchen", "Artikel → Seriennummern → Seriennummer (Teil genügt) → Suchen"),
        _weg("Nach einer Gutschrift wieder einlagern", "Artikel → Artikel & Bestand → Artikel öffnen → Seriennummern → Wieder einlagern",
             "Die Gutschrift ändert Seriennummern nicht; „Wieder einlagern“ holt eine verkaufte Nummer eines abgeschlossenen Belegs zurück, ohne den Bestand zu ändern.")],
       "lager:seriennummern", "Artikel → Seriennummern"),
    _h("reparaturbestand", "Wie funktioniert der Reparaturbestand (Retouren, Defektware)?",
       "reparaturbestand, defekte ware, retoure ins lager, ausbuchen, schrott, rückware, ware zurückbuchen, retoure, defektbestand",
       [_weg("Bei einer Gutschrift", "Gutschrift öffnen → Spalte B pro Lagerartikel mit Klick wählen: R (Reparaturbestand), B (Lagerbestand) oder – (keine Ware zurück) → Speichern: abschließen & Nummer vergeben",
             "Gebucht wird beim Abschließen, einmal. Ältere Gutschriften bucht man von Hand: Mehr → Ware ins Lager zurückbuchen."),
        _weg("Aus dem Reparaturbestand heraus", "Artikel → Artikel & Bestand → Artikel öffnen → Reparaturbestand: Menge und Bemerkung → In Lagerbestand umbuchen oder Ausbuchen",
             "Ausbuchen braucht eine Bemerkung (Schrott, Rückgabe an den Lieferanten).")],
       "lager:uebersicht", "Artikel → Artikel & Bestand",
       "Der Reparaturbestand ist ein eigener Bestand je Mandant: nicht verkaufbar, nicht im Lagerwert, nicht in der Inventur und nie unter 0."),
    _h("reservierung", "Was bedeutet die Spalte R (Reservierung) in der Bestellung?",
       "reservieren, reservierung, spalte r, r haken, verfügbar, bestand reserviert, ware blockiert, überbestellung, fremd reserviert, reserviert",
       [_weg("Haken setzen oder entfernen", "Verkauf → Kunde → Kunde wählen → Bestellung öffnen → bei der Position den Haken R setzen oder entfernen",
             "Standard ist gesetzt. Ohne Haken hält die Position keinen Lagerbestand fest, sie wartet auf den Wareneingang und der Bestand ist für andere frei. Der Haken wird nur von Hand gesetzt und entfernt, auch nach dem Wareneingang.")],
       "verkauf:start", "Verkauf → Kunde",
       "Was für eine Kundenbestellung reserviert ist, gilt für alle anderen als nicht vorhanden: Lieferschein und Rechnung ohne Lieferschein lassen sich nur abschließen, wenn Bestand minus fremde Reservierung reicht. Die Meldung nennt Bestand und reservierte Menge."),
    _h("esd_keys", "Wie verwalte und vergebe ich ESD-Keys (Softwarelizenzen)?",
       "esd, key, lizenz, software key, lizenzschlüssel, volumenkey, keys importieren, download key, esd keys",
       [_weg("Keys hinterlegen", "Artikel → ESD-Keys → Software-Artikel suchen und wählen → Keys importieren (eine Zeile je Key, Volumenkey als „KEY;Anzahl“) → Importieren"),
        _weg("Keys vergeben", "Lieferschein oder Rechnung abschließen vergibt vorhandene Keys; später zuordnen: Beleg öffnen → Symbol ESD-Keys → Keys manuell zuordnen → Zuordnen"),
        _weg("Keys an den Kunden mailen", "Beleg öffnen → Symbol ESD-Keys → Keys per E-Mail an den Kunden senden",
             "Ein Key lässt sich pro Zeile freigeben oder sperren.")],
       "esd:uebersicht", "Artikel → ESD-Keys",
       "ESD-Keys gibt es nur für Artikel der Kategorie Software. Fehlen beim Abschluss Keys, fragt das Programm; nach Bestätigung wird ohne die fehlenden Keys abgeschlossen und gedruckt, der Rest wird später unter ESD-Keys zugeordnet."),

    # ---- Werkstatt und Shop -------------------------------------------------------------------------------------
    _h("werkstatt", "Wie nehme ich ein Gerät zur Reparatur an (Werkstattauftrag)?",
       "werkstatt, reparatur, gerät annehmen, reparaturauftrag, annahme, annahmeschein, abholschein, gerät abholen, reparatur rechnung, werkstattauftrag",
       [_weg("Annahme", "Werkstatt → Annahme → Kunde, Gerät, Seriennummer, Zubehör und Fehlerbeschreibung eintragen → Annehmen → Annahmeschein drucken"),
        _weg("Bearbeiten und abrechnen", "Werkstatt → Reparaturen → Auftrag öffnen → Status wechseln (→ in Reparatur, → fertig – abholbereit …) → Teile und Arbeitszeit erfassen → Rechnung erstellen"),
        _weg("Abholen", "Auftrag öffnen → Status → abgeholt/ausgeliefert (Abholschein drucken)",
             "Ohne Berechnung abholen fragt das Programm vorher nach („Ja, ohne Berechnung abholen“). Im Garantiefall bucht „Garantie: Teile aus Lager abbuchen“ die Teile ab.")],
       "werkstatt:annahme", "Werkstatt → Annahme"),
    _h("reklamation", "Wie reklamiert ein Kunde ein Gerät (zurück in die Werkstatt)?",
       "reklamation, gerät reklamiert, garantiefall, kunde reklamiert, gewährleistung kunde, defekt zurück, reklamieren",
       [_weg("Aus dem Beleg", "Verkauf → Kunde → Kunde wählen → abgeschlossene Rechnung oder Lieferschein öffnen → Mehr → Gerät reklamiert → Werkstatt → Fehlerbeschreibung eintragen → Werkstattauftrag anlegen")],
       "verkauf:start", "Verkauf → Kunde"),
    _h("shop_abgleich", "Wie gleiche ich den Online-Shop ab (Bestellungen, Bestand, Preise)?",
       "shop, shopware, online shop, bestellungen abrufen, bestand senden, artikeldaten senden, shop abgleich, webshop, kategorien zuordnen, onlineshop",
       [_weg("Bestellungen holen", "Verkauf → Shop & Online → Bestellungen abrufen",
             "Shop-Bestellungen werden sofort nummeriert."),
        _weg("Bestand und Preise senden", "Verkauf → Shop & Online → Bestand & Preise senden",
             "Mehr als 50 Artikel auf einmal stoppen mit Meldung und gehen nur über „Bestand & Preise senden“ mit „alle Artikel“."),
        _weg("Artikeldaten senden (nur Admin)", "Verkauf → Shop & Online → Artikeldaten senden (optional nur Artikelnummer eintragen)",
             "Fällt „online“ weg oder wird der Artikel „nicht aktiv“, setzt der nächste Abgleich ihn im Shop auf inaktiv; gelöscht wird nichts."),
        _weg("Kategorien zuordnen (nur Admin)", "Verkauf → Shop & Online → Kategorien zuordnen → Vorhandene jetzt zuordnen oder Shop-Kategorie-ID eintragen → zuordnen")],
       "shop:uebersicht", "Verkauf → Shop & Online",
       "Der Shop selbst (Zugang, Takt, Preisstufen) wird in der Verwaltung unter Shops eingerichtet."),

    # ---- Stammdaten und Verwaltung ------------------------------------------------------------------------------
    _h("kunde_anlegen", "Wie lege ich einen neuen Kunden an oder ändere seine Daten?",
       "neukunde, kunde anlegen, kunde erfassen, neuer kunde, kundenstamm, kunde bearbeiten, kundendaten ändern, adresse ändern, ust-id prüfen",
       [_weg("Neukunde", "Verkauf → Kunde → Neukunde anlegen (Symbol neben der Suche) → Grunddaten mit Anschrift → Einstufung & Hinweis, Steuer & Bank → Speichern"),
        _weg("Daten ändern", "Verkauf → Kunde → Kunde wählen → Kundendaten bearbeiten → Speichern")],
       "verkauf:start", "Verkauf → Kunde",
       "Die Kundennummer kommt aus dem Kundenzähler (gilt für beide Mandanten); eine abgebrochene Maske verbraucht keine Nummer. Die USt-ID lässt sich im Reiter Steuer & Bank ab dem ersten Speichern prüfen."),
    _h("kunde_sperren", "Wie sperre ich einen Kunden?",
       "kunde sperren, sperre, gesperrt, kunde entsperren, keine neuen belege, zahlungsunfähig, schlechter zahler, kunde blockieren",
       [_weg("Sperren oder entsperren", "Verkauf → Kunde → Kunde wählen → Kundendaten bearbeiten → Reiter Einstufung & Hinweis → Haken „gesperrt“ setzen oder entfernen → Speichern",
             "Bei einem gesperrten Kunden sind keine neuen Belege möglich (die Kundenseite zeigt eine Warnung). Ein laufender Vorgang lässt sich weiterführen, z. B. Angebot → Bestellung → Lieferschein oder Rechnung.")],
       "verkauf:start", "Verkauf → Kunde"),
    _h("mandant_wechseln", "Wie wechsle ich den Mandanten (OKAY / ZONE)?",
       "mandant, firma wechseln, okay, zone, mandant wechseln, firma umschalten, mandantenwahl, mandanten",
       [_weg("Im Kopf der Seite", "Oben auf den Firmennamen klicken (▾) → Mandant wählen"),
        _weg("Per Schnellbedienung", "Strg+K → „Mandant wechseln“ tippen → Enter",
             "Die Wahl erscheint nur, wenn mehr als ein Mandant zur Verfügung steht.")],
       "dashboard", "Dashboard",
       "Belege, Nummernkreise, Zahlungen und Werkstattaufträge gelten je Mandant; Kunden, Lieferanten und Artikel gelten für beide."),
    _h("passwort", "Wie ändere ich mein Passwort?",
       "passwort, kennwort, passwort vergessen, passwort ändern, login, anmelden, abmelden, passwort zurücksetzen",
       [_weg("Eigenes Passwort", "Oben auf den eigenen Benutzernamen klicken → Passwort ändern"),
        _weg("Passwort vergessen", "Ein Admin: Verwaltung (Zahnrad oben) → Benutzer → Benutzer wählen → neues Passwort setzen → Speichern",
             "Es gibt keinen Passwort-Reset per Mail; ein vergessenes Passwort setzt ein Admin in der Verwaltung neu.")],
       "password_change", "Passwort ändern"),
    _h("benutzer", "Wie lege ich einen Benutzer an oder vergebe Rechte?",
       "benutzer anlegen, neuer mitarbeiter, user, admin rechte, mitarbeiter, berechtigung, benutzerverwaltung, zugang, benutzer",
       [_weg("Benutzer anlegen", "Verwaltung (Zahnrad oben, nur für Admins) → Benutzer → Benutzer hinzufügen → Name und Passwort eintragen → Speichern → Rechte setzen → Speichern",
             "Es gibt zwei Stufen: Admin (Administrator oder Mitarbeiter mit Verwaltungszugang) und Mitarbeiter. Ansehen darf jeder angemeldete Benutzer; Änderungen wie Zahlungen stornieren, Verrechnungen aufheben oder Shop-Artikeldaten senden nur Admins.")],
       "admin:auth_user_changelist", "Verwaltung → Benutzer"),
    _h("mailvorlagen", "Wie ändere ich die Mailvorlagen für Belege?",
       "mailvorlage, mail text, e-mail vorlage, betreff, standardtext mail, mailtext ändern, signatur, mailvorlagen",
       [_weg("Vorlage ändern", "Verwaltung (Zahnrad oben) → Mailvorlagen → Vorlage (Mandant und Art) öffnen → Betreff und Text ändern → Speichern",
             "Je Mandant und Mailart gibt es genau eine Vorlage; fehlende entstehen beim Öffnen mit dem Standardtext. Die erlaubten Platzhalter stehen neben dem Text.")],
       "admin:verkauf_mailvorlage_changelist", "Verwaltung → Mailvorlagen"),
    _h("nummernkreise", "Wie stelle ich Belegnummern und Nummernkreise ein?",
       "nummernkreis, belegnummer, laufende nummer, nummer ändern, nächste nummer, rechnungsnummer, nummer startet, nummernkreise, kundennummer, artikelnummer",
       [_weg("Nächste Nummer einstellen", "Verwaltung (Zahnrad oben) → Laufende Nummern → Eintrag wählen → nächste Nummer eintragen → Speichern",
             "Das Format bleibt. Belegnummern zählen je Mandant und laufen über den Jahreswechsel weiter, nur die Jahreszahl ändert sich. Vergebene Nummern kommen nie wieder: Die nächste muss über der höchsten vergebenen liegen (auch den früheren Nummern umgewandelter Belege). Rechnungen und Gutschriften sind lückenlos und nur einstellbar, solange im laufenden Jahr noch keine Nummer vergeben wurde. Artikel, Kunden und Lieferanten haben je einen Zähler für beide Mandanten.")],
       "admin:verkauf_nummernkreis_changelist", "Verwaltung → Laufende Nummern"),
)


# ---- Suche ------------------------------------------------------------------------------------------------------

def woerter(text: str) -> list:
    """Kleingeschrieben, Umlaute als ae/oe/ue/ss, nur Buchstaben und Ziffern."""
    return re.findall(r"[a-z0-9]+", (text or "").lower().translate(_UMSCHRIFT))


def ist_frage(text: str) -> bool:
    """Beginnt mit wie/wo/was/wann/kann/warum … oder endet mit „?“."""
    text = (text or "").strip()
    erste = woerter(text)[:1]
    return text.endswith("?") or bool(erste and erste[0] in FRAGE_ANFAENGE)


def _aehnlich(a: str, b: str) -> bool:
    """Gleicher Wortanfang (Beugung: buche/buchen, mahnung/mahnungen); kurze Wörter nur exakt."""
    if len(a) < 4 or len(b) < 4:
        return False
    kurz, lang = min(len(a), len(b)), max(len(a), len(b))
    gemeinsam = 0
    for x, y in zip(a, b):
        if x != y:
            break
        gemeinsam += 1
    return gemeinsam >= max(4, kurz - 2) and kurz >= 0.6 * lang


@lru_cache(maxsize=1)
def _index():
    """Je Eintrag: Wörter der Frage, Wörter der Stichworte und die mehrwortigen Stichworte als Mengen (einmal berechnet)."""
    index = []
    for eintrag in HILFE:
        frage = set(woerter(eintrag.frage)) - STOPPWOERTER
        stich, phrasen = set(), []
        for s in eintrag.stichworte:
            teile = [t for t in woerter(s) if t not in STOPPWOERTER]
            stich.update(teile)
            if len(teile) >= 2:
                phrasen.append(frozenset(teile))
        index.append((eintrag, frozenset(frage), frozenset(stich), tuple(phrasen)))
    return tuple(index)


def _punkte(wort, frage, stich):
    """Beste Wertung eines Suchworts: exakt in der Frage 4, exakt im Stichwort 3, gleicher Wortanfang 3/2, enthalten 1."""
    if wort in frage:
        return 4
    if wort in stich:
        return 3
    if any(_aehnlich(wort, t) for t in frage):
        return 3
    if any(_aehnlich(wort, t) for t in stich):
        return 2
    if len(wort) >= 6 and any(len(t) >= 6 and (t in wort or wort in t) for t in (*frage, *stich)):
        return 1  # Zusammensetzungen: „kundenrechnung“ enthält „rechnung“
    return 0


def hilfe_suchen(text: str, maximal: int = MAX_HILFE_TREFFER) -> list:
    """Passende Hilfe-Einträge, beste zuerst (bei gleicher Wertung die Reihenfolge der Liste); höchstens `maximal`."""
    suchwoerter = [w for w in dict.fromkeys(woerter(text)) if w not in STOPPWOERTER and len(w) >= 2]
    if not suchwoerter:
        return []
    gesucht = frozenset(suchwoerter)
    ergebnis = []
    for reihenfolge, (eintrag, frage, stich, phrasen) in enumerate(_index()):
        einzeln = [_punkte(w, frage, stich) for w in suchwoerter]
        treffer = sum(1 for p in einzeln if p)
        if treffer * 2 < len(suchwoerter):
            continue  # weniger als die Hälfte der Wörter passt
        summe = sum(einzeln) + (3 if any(p <= gesucht for p in phrasen) else 0)
        if summe >= MIN_PUNKTE:
            ergebnis.append((-summe, reihenfolge, eintrag))
    ergebnis.sort(key=lambda z: z[:2])
    return [e for _, _, e in ergebnis[:maximal]]


def als_treffer(eintrag: Hilfe) -> dict:
    """Darstellung für die Palette (JSON): Gruppe „Hilfe“, Titel = Frage, Wege mit Schritten, Sprungadresse."""
    url = eintrag.url()
    return {
        "gruppe": "Hilfe", "titel": eintrag.frage, "zusatz": "Anleitung", "url": url,
        "hilfe": {"ziel": eintrag.ziel, "hinweis": eintrag.hinweis,
                  "wege": [{"titel": w.titel, "schritte": list(w.schritte), "hinweis": w.hinweis} for w in eintrag.wege]},
    }
