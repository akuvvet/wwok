# Pflichtenheft – Web-Warenwirtschaft (Mention-Ersatz)

> Stand 24.10.2026, exportiert aus dem Claude-Dokument „Pflichtenheft Web-Warenwirtschaft“. Bei Änderungen bitte dort pflegen und hier neu exportieren.

Webbasierte, mandantenfähige Warenwirtschaft für OKAY Computer und ZONE Handel, erreichbar über den Browser (HTTPS), betrieben auf dem eigenen Server.

## 1. Grundprinzipien

- Webanwendung, Zugriff per Browser, ausschließlich HTTPS
- Mandantenfähig (z. B. OKAY Computer, ZONE Handel)
- Benutzerkonten mit Rechten, Zwei-Faktor-Login
- GoBD-konform: abgeschlossene Belege gesperrt, Korrektur nur per Storno/Gutschrift, alle Änderungen protokolliert, revisionssichere Archivierung
- Automatische tägliche Datenbanksicherung
- Bedienung per Tastatur möglich (Enter übernimmt, Escape schließt Fenster)

## 2. Mandanten

| Bereich | Gemeinsam | Pro Mandant getrennt |
| --- | --- | --- |
| Kunden (gleiche Kundennummer, gleiche Ansprechpartner) | ✔ |  |
| Lieferanten | ✔ | eigene Kundennummer beim Lieferanten, eigene Ansprechpartner |
| Artikel, Kategorien, Lieferanten-Preislisten | ✔ |  |
| Einkaufskonditionen, letzter EK, Bestellvorschlag |  | ✔ |
| Lagerbestände, Reservierungen, Inventur |  | ✔ |
| Belege und Nummernkreise |  | ✔ |
| Offene Posten, Mahnwesen, Kundenkontoauszug, Kreditlimit |  | ✔ |
| ESD-Key-Bestand |  | ✔ |
| Werkstatt, Kassenbuch |  | ✔ |
| Firmendaten, Briefbogen |  | ✔ |
| Shops/Marktplätze |  | je Shop einem Mandanten zugeordnet |
| Newsletter-Einwilligung |  | ✔ |

Hinweis: Gemeinsame Nutzung der Kundendaten durch zwei Firmen rechtlich absichern (z. B. Vereinbarung über gemeinsame Verantwortung, mit Datenschutzberater klären).

## 3. Artikeltypen

| Typ | Bedeutung | Lagerbestand |
| --- | --- | --- |
| A – Artikel | Normale Produkte, Ein- und Verkauf | ja |
| P – Preisliste | Lieferantenprodukte aus importierten Preislisten, ohne eigene Artikelanlage verkaufbar | nein (wird bei Wareneingang automatisch zu A) |
| L – Leistung | Dienstleistungen | nein |
| G – Gruppe | Zusammengesetzte Position: Überschrift mit Gesamtpreis netto/brutto, Unterartikel ohne Einzelpreise | aus Komponenten berechnet |

- G-Positionen werden pro Angebot frei zusammengestellt; Gesamtpreis automatisch berechnet, manuell überschreibbar
- G-Artikel optional als Einzelprodukt in den Shop hochladbar; Bestand aus den Komponenten im Lager des Shop-Mandanten; beim Verkauf werden die Komponenten abgebucht

## 4. Hauptbereiche

1. Verkaufsabwicklung
2. Einkaufsabwicklung
3. Bearbeitung (Finanzen und Lager)
4. Stammdaten
5. Werkstatt
6. Auswertungen
7. Voreinstellungen
8. Startseite (fällige Wiedervorlagen, offene Aufträge, fällige Mahnungen)

## 5. Verkaufsabwicklung

### Einstieg

- Kundensuche (Name, Nummer; Platzhalter `*` zeigt alle bzw. `Mül*`)
- Button „Neukunde anlegen“ (mit Dublettenprüfung)
- Freie Belegsuche ohne Kundenauswahl

### Ablauf nach Kundenauswahl

1. Ansprechpartnerfenster → Ansprechpartner wählen
2. Belegfenster mit Filter: Alle, Angebote, Aufträge, Lieferscheine, Nachlieferscheine, Rechnungen, Gutschriften, offene Belege; Mehrfachauswahl zum gemeinsamen Senden/Drucken; verlassen per Auswahl, Schließen oder Escape
3. Abwicklungsmaske des Kunden

### Abwicklungsmaske

- Kopfbereich: Ansprechpartner, E-Mail, Telefon, Adresse, Kartensymbol (öffnet Karte)
- Kundendaten direkt bearbeitbar
- Button: Belege dieses Kunden suchen
- Button: globale Belegsuche
- Wiedervorlage (Datum + Notiz, ohne Mitarbeiterzuordnung)
- Warnung bei Kreditlimit-Überschreitung oder Kundensperre
- Neuen Beleg anlegen

### Positionserfassung

| Feld | Beschreibung |
| --- | --- |
| Typ | Links, Auswahl A/P/L/G, Standard A |
| Artikel | Suche nach Artikelnummer oder Name |
| Menge | Stückzahl |
| Preis | Vorschlag aus VK-Stufe/Sonderpreis, frei änderbar; Anzeige letzter Preis dieses Kunden mit Datum |
| Übernehmen | Enter oder Button, danach nächste Zeile |

Positionsdetails (z. B. Doppelklick): Zusatztext, Seriennummern, Rabatt.

### Abschluss (Detailfenster)

- Zahlungsbedingung, Versandart, Versandanschrift, Rechnungsanschrift (vorbelegt aus Kundendaten)
- Versandkosten, Verpackung, Versicherung
- Beleg speichern, Nummer vergeben, drucken, als PDF/E-Rechnung mailen

### Nach Abschluss einer Rechnung

- Beträge, Positionen und Versandkosten gesperrt
- Nur Infos wie Versandart/Zahlungsbedingung änderbar, mit Protokoll
- Korrektur über Storno/Gutschrift

### Belegfluss

- Angebot → Auftrag/Kundenbestellung → Lieferschein → Rechnung (per Klick)
- Angebot in Lieferantenbestellung kopieren (P-Artikel automatisch dem Lieferanten zugeordnet)
- Proforma-Rechnung (ohne Buchung)
- Teillieferung: Geliefertes wird berechnet, Rest bleibt als offene Bestellung verknüpft
- Nachlieferschein: aus Rechnung kopieren, Menge anpassen, keine neue Rechnung; System fragt, ob vom Lager abgebucht wird
- Kundenretoure und Gutschrift
- Anzahlungen/Teilzahlungen auf Bestellung buchbar; Bestellung und Rechnung zeigen bezahlten und offenen Restbetrag
- Wiederkehrende Rechnungen (Wartung, Hosting, Lizenzen)
- Barverkauf als Rechnung mit Zahlungsart „bar“
- Kundenbestellungen per Excel/CSV importieren (eine allgemeine, feste Spaltenzuordnung)

## 6. Einkaufsabwicklung

Aufbau wie Verkauf, mit Lieferanten: Lieferantensuche, „Neuer Lieferant“, Belegsuche, Ansprechpartner pro Mandant, eigene Kundennummer beim Lieferanten pro Mandant.

- Lieferantenbestellung mit Anzeige letzter EK (pro Mandant) und aktuellem Preislistenpreis
- Wareneingang (Barcode-Scan per Bluetooth-Scanner), Teillieferungen/Rückstände
- Lieferantenrechnung erfassen und mit Wareneingang verknüpfen (beidseitig nachvollziehbar), Abgleich Menge/Preis mit Warnung
- E-Rechnungen empfangen: ZUGFeRD/XRechnung einlesen und als Lieferantenrechnung anlegen
- Rücksendung an Lieferanten mit Gutschrift
- RMA/Garantiefälle: RMA-Nummer, Status bis Ersatz/Gutschrift, verknüpft mit Werkstatt
- Bestellvorschlag: fester Mindestbestand pro Artikel, sonst aus Verkaufsverlauf; per Klick Bestellung beim günstigsten Lieferanten

## 7. Lieferantenprodukte und Preise

- Preislisten-Import (CSV, FTP, Schnittstelle), automatische Aktualisierung von Preisen und Beständen per Zeitplan
- Verkaufspreisstufen: VK1 Endkunden, VK2 gewerbliche Endkunden, VK3 Händler
- VK als Aufschlag auf EK: global, pro Kategorie, pro Artikel überschreibbar
- Kundensonderpreise pro Kunde und Artikel

**Importprofil je Lieferanten-Preisliste** (einmal einrichten, danach automatisch genutzt):

| Einstellung | Inhalt |
| --- | --- |
| Quelle | Datei-Upload, FTP/SFTP-Abruf oder Schnittstelle, mit Zeitplan |
| Dateityp | CSV, TXT, Excel (XLSX/XLS), XML |
| Format | Trennzeichen, Textbegrenzer, Zeichensatz (UTF-8, ANSI), Dezimaltrennzeichen, Kopfzeile ja/nein, erste Datenzeile |
| Feldzuordnung | Spalte der Datei → Feld: Lieferanten-Bestellnummer, Herstellernummer, EAN, Bezeichnung, Hersteller, Kategorie, EK netto, Bestand, Lieferzeit, Gewicht |
| Vorschau | erste Zeilen der Datei mit Zuordnung anzeigen, bevor importiert wird |
| Abgleich | Verknüpfung mit vorhandenen Artikeln über Herstellernummer, ersatzweise EAN |
| Protokoll | Anzahl neu, geändert, Fehler; fehlerhafte Zeilen einsehbar |

## 8. Bearbeitung (Finanzen und Lager)

### Finanzen

- Offene Posten Kunden und Lieferanten
- Zahlungseingänge und -ausgänge buchen, Teilzahlungen
- Bankauszug-Import (CAMT/CSV), automatische Zuordnung zu offenen Rechnungen
- SEPA-XML-Export für Lieferantenzahlungen (StarMoney), mit Skontofristen
- Kundenkontoauszug (einzeln und für alle Kunden)
- Mahnwesen: Zahlungserinnerung, 1. und 2. Mahnung, Fristen/Gebühren einstellbar, Mahnungen drucken
- Kassenbuch (gesperrt, protokolliert) – TSE-Pflicht mit Steuerberater klären
- DATEV-Export

### Lager

- Bestandsänderungen: Eigenverbrauch, Eigenbestand, Umbuchungen
- Bestandshistorie pro Artikel (Datum, Menge, Grund, Beleg)
- Reservierung für offene Bestellungen; Anzeige Gesamt/reserviert/verfügbar
- Inventur: Zählliste, Erfassung, automatische Differenzbuchung
- Lagerbewertung über Durchschnitts-EK
- Lagerplätze (Regal/Fach)
- Seriennummern durchgängig vom Wareneingang bis Werkstatt

## 9. Stammdaten

- Kunden (VK-Stufe, Kreditlimit pro Mandant, Sperre, Newsletter-Einwilligung pro Firma, USt-ID mit EU-Prüfung)
- Lieferanten
- Artikel, Leistungen, Preislisten
- Kategorien mehrstufig (z. B. Prozessoren > Intel/AMD > Sockel/Serie)

### 9.1 Feldliste Artikelstamm

| Bereich | Felder |
| --- | --- |
| Grunddaten | Bezeichnung, erweiterte Bezeichnung (zugleich Langbeschreibung im Shop, keine separate Langbeschreibung), Kategorie, Hersteller, Herstellernummer, EAN, Checkbox „nicht aktiv“, Garantie in Monaten |
| Maße | Länge, Breite, Höhe, Gewicht in kg |
| Preise | letzter EK pro Mandant, Durchschnitts-EK, VK1–VK3 mit Aufschlag, Steuersatz |
| Online | bis zu 10 Bilder (erstes = Hauptbild, für Shop/eBay/Kaufland), eine Checkbox „online“ für alle Kanäle, Kurzbeschreibung, Keywords, Metatitel, Meta-Description, Meta-Keywords |
| Lieferanten | Liste je Lieferant: Preis, Bestand, Herkunfts-Preisliste, Bestellnummer des Lieferanten; Verknüpfung über Herstellernummer; Bestellnummer wird automatisch in die Lieferantenbestellung übernommen |

### 9.2 Feldliste Kundenstamm

| Bereich | Felder |
| --- | --- |
| Grunddaten | Kundennummer, Name 1–3, Straße, PLZ, Ort, Land, Webseite, Gesellschaftsform, Inhaber bzw. Geschäftsführer |
| Einstufung | Kundenart (Endkunde, gewerblicher Endkunde, Wiederverkäufer), Preisstufe VK1–VK3 getrennt davon einstellbar |
| Automatisch | Datum Erstkontakt beim Anlegen, letzte Änderung mit Datum und Benutzer |
| Hinweis | Hinweisfeld; wird beim Öffnen des Kunden automatisch angezeigt, wenn gefüllt |
| Vorgaben für Belege | Standard-Zahlungsbedingung (im Beleg änderbar), abweichende Rechnungsanschrift, mehrere Versandanschriften, eine davon als Standard |
| Ansprechpartner | mehrere: Name, Abteilung/Position, E-Mail, Telefon, Durchwahl, Mobil, eigener Onlineshop-Zugang (Konto wird in Shopware angelegt, Passwort setzt der Kunde per Link, keine Klartext-Passwörter) |
| Finanzen | USt-ID, Kreditlimit pro Mandant, Bankverbindung, SEPA-Mandat mit Mandatsreferenz und Datum |

### 9.3 Feldliste Lieferantenstamm

Aufbau wie der Kundenstamm (Grunddaten, Ansprechpartner, Bankverbindung, USt-ID), zusätzlich:

| Bereich | Felder |
| --- | --- |
| Pro Mandant | eigene Kundennummer beim Lieferanten, eigene Ansprechpartner |
| Konditionen | Zahlungsziele mit Skonto (für SEPA-Export), Mindestbestellwert, Lieferzeit, versandkostenfrei ab Betrag |

## 10. ESD-Key-Verwaltung

- Key-Import, Bestand pro Mandant
- Automatische Zuordnung beim Verkauf zum Beleg, Versand per Mail/auf Rechnung
- Volumenkeys mit maximaler Vergabeanzahl (z. B. 10), danach gesperrt
- Gleicher Key nicht zweimal an denselben Kunden, außer Verkäufer entscheidet
- Bei Retoure individuelle Entscheidung: Key freigeben oder sperren

## 11. Werkstatt

- Geräteannahme (Seriennummer, Fehlerbeschreibung), Abholschein
- Status (angenommen, in Reparatur, fertig), Statusmail an Kunden
- Teile und Arbeitszeit erfassen, Rechnung oder Garantie
- Übergang in Lieferanten-RMA

## 12. Versand

- DHL Paket und Deutsche Post (zuerst), später GLS, DPD, UPS
- Etiketten aus dem Lieferschein, Sendungsnummer am Beleg und in Kundenmail

## 13. Schnittstellen

- Shopware 6 (mehrere Shops, je einem Mandanten zugeordnet): Bestellimport, Bestands-/Preisabgleich, Kategorien
- eBay, Kaufland
- Nur verfügbarer Bestand wird gemeldet (Schutz vor Doppelverkauf)
- Newsletter mit Double-Opt-in und Abmeldelink

## 14. E-Rechnung und Steuern

- Ausgang: ZUGFeRD (PDF mit XML), optional XRechnung
- Eingang: ZUGFeRD/XRechnung einlesen
- Reverse Charge (EU) mit USt-ID-Prüfung, optional Differenzbesteuerung

## 15. Auswertungen

- Umsatz, Marge, Topseller pro Mandant und Zeitraum, Excel-Export

## 16. Voreinstellungen

- Mandanten mit Firmendaten und Briefbogen
- Nummernkreise, Steuersätze
- Versandarten mit Standardkosten
- Zahlungsbedingungen (z. B. Vorkasse, 14 Tage 2 % Skonto)
- Mahnstufen, Fristen, Gebühren
- E-Mail-Server, Mailvorlagen, Textbausteine
- Importformat für Bestellungen
- Versanddienst-Zugangsdaten
- Shop-/Marktplatz-Zugangsdaten
- Globale Aufschläge VK1–VK3
- Benutzer und Rechte

## 17. Datenschutz

- Kundendaten auf Anfrage sperren/anonymisieren; Rechnungen bleiben wegen Aufbewahrungspflicht erhalten

## 18. Offene Klärungen

- TSE-Pflicht beim Barverkauf (Steuerberater)
- Gemeinsame Kundendaten beider Firmen (Datenschutzberater)
- Technologie-Stack (z. B. Python/Django oder Node, MySQL) und Umsetzungsreihenfolge
