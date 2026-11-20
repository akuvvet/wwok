# wwok – Web-Warenwirtschaft für OKAY Computer und ZONE Handel

Eigene, webbasierte Warenwirtschaft als Ersatz für **Mention**. Mandantenfähig für **OKAY Computer** und **ZONE Handel**, im Browser über HTTPS erreichbar und auf dem eigenen Strato V-Server betrieben.

Diese Datei fasst **alles** zusammen, was bisher besprochen und entschieden wurde: Vorgeschichte, alle Anforderungen, Entscheidungen, Technik, Server, GitHub, die bisherigen Einrichtungsschritte mit allen aufgetretenen Problemen, den aktuellen Stand und die offenen Punkte.

| Was | Wo |
| --- | --- |
| Repository | `akuvvet/wwok` (privat) |
| Lokaler Ordner | `D:\Desktop\github\wwok` |
| Server | Strato V-Server, Ubuntu 22.04, Plesk, Python 3.12 für wwok (eigene Umgebung venv312; System-Python bleibt 3.10.12) |
| Ziel-Adresse | `https://erp.okaycomputer.de` |
| Pflichtenheft im Repo | [`docs/pflichtenheft.md`](docs/pflichtenheft.md) |
| Pflichtenheft (Original, editierbar) | Claude-Dokument „Pflichtenheft Web-Warenwirtschaft“: https://claude.ai/artifact/BDJTVmgcQwKVpeas77Bk7W |
| Server-Anleitung | [`deploy/PLESK.md`](deploy/PLESK.md) |
| Stand dieser Datei | 25.10.2026 – Anwendung läuft produktiv auf dem Server, siehe [Abschnitt 15](#15-aktueller-stand) |

---

## Inhalt

1. [Vorgeschichte: Excel-Rechnungen und E-Rechnung](#1-vorgeschichte-excel-rechnungen-und-e-rechnung)
2. [Ziel des Projekts](#2-ziel-des-projekts)
3. [Alle Entscheidungen auf einen Blick](#3-alle-entscheidungen-auf-einen-blick)
4. [Anforderungen im Detail](#4-anforderungen-im-detail)
5. [Feldlisten Stammdaten](#5-feldlisten-stammdaten)
6. [Prüfdurchgänge und Ergänzungen](#6-prüfdurchgänge-und-ergänzungen)
7. [Offene Klärungen](#7-offene-klärungen)
8. [Technik und Aufbau des Codes](#8-technik-und-aufbau-des-codes)
9. [Server und Subdomain](#9-server-und-subdomain)
10. [GitHub](#10-github)
11. [Lokal starten (Windows)](#11-lokal-starten-windows)
12. [Erstes Hochladen zu GitHub – so ist es gelaufen](#12-erstes-hochladen-zu-github--so-ist-es-gelaufen)
13. [Server-Einrichtung Schritt für Schritt](#13-server-einrichtung-schritt-für-schritt)
14. [Weiterarbeiten mit Claude](#14-weiterarbeiten-mit-claude)
15. [Aktueller Stand](#15-aktueller-stand)
16. [Nächste Schritte](#16-nächste-schritte)
17. [Regeln für das Repository](#17-regeln-für-das-repository)
18. [Fachregeln im Programm (verbindlich)](#18-fachregeln-im-programm-verbindlich)
19. [Betrieb: Datenimporte, Zeitpläne, Geheimnisse, Tests](#19-betrieb-datenimporte-zeitpläne-geheimnisse-tests)

---

## 1. Vorgeschichte: Excel-Rechnungen und E-Rechnung

- Rechnungen werden bisher in **Excel** geschrieben.
- Ab **2027** wird die E-Rechnung im B2B-Bereich Pflicht.
  - **ZUGFeRD** = PDF mit eingebetteter XML-Datei (für Menschen lesbar und maschinell auswertbar).
  - **XRechnung** = reine XML-Datei, vor allem für Behörden.
- **Entscheidung:** Vorerst bei Excel bleiben und per **Python-Skript** (z. B. Bibliothek `factur-x`) aus der Excel-Rechnung eine ZUGFeRD-Rechnung erzeugen.
- Voraussetzung: Alle Pflichtangaben stehen in der Excel-Vorlage in **festen Zellen**.
- **Offen:** Excel-Vorlage an Claude schicken, damit das Skript gebaut werden kann.
- Daraus entstand die Idee, Mention komplett durch eine eigene Web-Warenwirtschaft zu ersetzen, die E-Rechnungen direkt erzeugt.

## 2. Ziel des Projekts

- Bisher im Einsatz: **Mention** als Warenwirtschaft.
- Ziel: eine ähnliche Software selbst schreiben, als **Online-Portal**, über den Browser erreichbar.
- Gewünschte Kernmodule aus dem ersten Gespräch:
  - Kunden, Artikel, Lager, Lieferanten
  - Wareneingang, Zahlungseingang
  - Zahlungsausgang an Lieferanten als **SEPA-XML** (z. B. für StarMoney)
  - Lieferantenprodukte: Preislisten importieren, daraus Angebote erstellen, **ohne** eigene Artikelanlage
  - Preise und Bestände automatisch über Lieferantendaten holen und aktualisieren
  - Rechnungen als **PDF** versenden
  - Newsletter, Gruppen, Bestellungen
  - Zusammengesetzte Artikel (Positionsgruppen)

## 3. Alle Entscheidungen auf einen Blick

| Thema | Entscheidung |
| --- | --- |
| Rechnungen bis zur neuen Software | Excel bleibt, ZUGFeRD per Python-Skript |
| Art der Software | Webanwendung, nur HTTPS, eigener Server |
| Mandanten | OKAY Computer und ZONE Handel in einem System |
| Kunden | **gemeinsam** für beide Firmen, gleiche Kundennummer, gleiche Ansprechpartner (keine mandantenspezifischen Ansprechpartner bei Kunden) |
| Lieferanten | gemeinsam, aber eigene Kundennummer beim Lieferanten und eigene Ansprechpartner **pro Mandant** |
| Lagerbestände | pro Mandant getrennt (Einkauf für OKAY wird bei OKAY gebucht, für ZONE bei ZONE) |
| Artikeltyp in der Positionszeile | Auswahl links, Standard **A** |
| Abgeschlossene Rechnung | Beträge, Positionen, Versandkosten gesperrt; nur Infos wie Versandart/Zahlungsbedingung änderbar, mit Protokoll; Korrektur per Storno/Gutschrift |
| Wiedervorlage | Datum + Notiz, **ohne** Mitarbeiterzuordnung |
| Import Kundenbestellungen | **eine** allgemeine, feste Spaltenzuordnung für alle Kunden (nicht pro Kunde) |
| Kasse | wenige Barverkäufe, bisher Excel; künftig Barverkauf als normale Rechnung mit Zahlungsart „bar“ plus gesperrtes Kassenbuch, **keine** eigene TSE-Kasse |
| Marktplätze | Shopware 6, eBay, Kaufland – **Amazon nicht benötigt** |
| Versand | zuerst DHL Paket und Deutsche Post (meistgenutzt), später GLS, DPD, UPS |
| Online-Freigabe Artikel | **eine** Checkbox „online“ für alle Kanäle gemeinsam |
| Langbeschreibung Shop | die **erweiterte Bezeichnung** dient als Langbeschreibung, keine separate Langbeschreibung |
| Verknüpfung Lieferantenartikel | über die **Herstellernummer** |
| Kundenart und Preisstufe | **getrennt** einstellbar (z. B. Endkunde mit Händlerpreisen möglich) |
| Onlineshop-Zugang der Ansprechpartner | Konto wird in Shopware angelegt, Kunde setzt Passwort per Link, keine Klartext-Passwörter |
| Technik | Python + Django + MySQL |
| Subdomain | `erp.okaycomputer.de`, eigenes Verzeichnis neben dem Shop, nicht in `httpdocs` |
| Datenbank | eigene Datenbank `wwok` mit eigenem Benutzer, **nicht** die Shopware-Datenbank |
| Projektname | erst `wwkvt` (Repo `kuvvet/wwkvt` geplant), dann **`wwok`** |
| Repository | **`akuvvet/wwok`**, privat |
| Lokaler Ordner | `D:\Desktop\github\wwok` |

## 4. Anforderungen im Detail

### 4.1 Grundprinzipien

- Webanwendung, Zugriff per Browser, **ausschließlich HTTPS**
- Mandantenfähig (OKAY Computer, ZONE Handel)
- Benutzerkonten mit Rechten und **Zwei-Faktor-Login**
- **GoBD-konform:** abgeschlossene Belege gesperrt, Korrektur nur per Storno/Gutschrift, alle Änderungen protokolliert, revisionssichere Archivierung
- Automatische **tägliche Datenbanksicherung**
- Bedienung per Tastatur: **Enter** übernimmt, **Escape** schließt Fenster

### 4.2 Mandanten – was ist gemeinsam, was getrennt

| Bereich | Gemeinsam | Pro Mandant getrennt |
| --- | --- | --- |
| Kunden (gleiche Kundennummer, gleiche Ansprechpartner) | ✔ | |
| Lieferanten | ✔ | eigene Kundennummer beim Lieferanten, eigene Ansprechpartner |
| Artikel, Kategorien, Lieferanten-Preislisten | ✔ | |
| Einkaufskonditionen, letzter EK, Bestellvorschlag | | ✔ |
| Lagerbestände, Reservierungen, Inventur | | ✔ |
| Belege und Nummernkreise | | ✔ |
| Offene Posten, Mahnwesen, Kundenkontoauszug, Kreditlimit | | ✔ |
| ESD-Key-Bestand | | ✔ |
| Werkstatt, Kassenbuch | | ✔ |
| Firmendaten, Briefbogen | | ✔ |
| Shops/Marktplätze | | je Shop einem Mandanten zugeordnet |
| Newsletter-Einwilligung | | ✔ (pro Firma) |

Hinweis: Die gemeinsame Nutzung der Kundendaten durch zwei Firmen muss rechtlich abgesichert werden (z. B. Vereinbarung über gemeinsame Verantwortung, mit Datenschutzberater klären).

### 4.3 Artikeltypen

| Typ | Bedeutung | Lagerbestand |
| --- | --- | --- |
| **A** – Artikel | normale Produkte, Ein- und Verkauf | ja |
| **P** – Preisliste | Lieferantenprodukte aus importierten Preislisten, ohne eigene Artikelanlage verkaufbar | nein; ein eigener Artikel A entsteht nur **nach Rückfrage** im Wareneingang (Abschnitt 18) |
| **L** – Leistung | Dienstleistungen | nein |
| **G** – Gruppe | zusammengesetzte Position: Überschrift mit Gesamtpreis netto/brutto, darunter Unterartikel **ohne** Einzelpreise | aus den Komponenten berechnet |

- G-Positionen werden **pro Angebot frei zusammengestellt**. Der Gesamtpreis wird automatisch berechnet und ist manuell überschreibbar.
- G-Artikel können optional als **Einzelprodukt in den Onlineshop** hochgeladen werden. Der Bestand ergibt sich aus den Komponenten im Lager des Shop-Mandanten. Beim Verkauf werden die Komponenten abgebucht.

### 4.4 Hauptbereiche

1. Verkaufsabwicklung
2. Einkaufsabwicklung
3. Bearbeitung (Finanzen und Lager)
4. Stammdaten
5. Werkstatt
6. Auswertungen
7. Voreinstellungen
8. Startseite (fällige Wiedervorlagen, offene Bestellungen, fällige Mahnungen)

### 4.5 Verkaufsabwicklung

**Einstieg**

- Kundensuche nach Name oder Nummer
- Platzhalter `*` zeigt alle Kunden, `Mül*` alle, die mit „Mül“ beginnen
- Button **„Neukunde anlegen“** (mit Dublettenprüfung)
- **Freie Belegsuche** ohne Kundenauswahl

**Ablauf nach der Kundenauswahl**

1. **Ansprechpartnerfenster** öffnet sich → Ansprechpartner wählen
2. **Belegfenster** mit den gespeicherten Belegen des Kunden
   - Filter: alle, Angebote, Bestellungen, Lieferscheine, Nachlieferscheine, Rechnungen, Gutschriften, **offene Belege**
   - Mehrfachauswahl, um mehrere Belege gemeinsam zu **senden oder zu drucken**
   - Verlassen per Auswahl, Fenster schließen oder **Escape**
3. **Abwicklungsmaske** des Kunden

**Abwicklungsmaske**

- Kopfbereich: Ansprechpartner, E-Mail, Telefon, Adresse
- Kleines **Kartensymbol**, das die Kundenadresse auf der Karte zeigt
- Kundendaten (E-Mail, Telefon usw.) **direkt in der Maske änderbar**
- Button **„Belege dieses Kunden“** – jederzeit suchen, ohne die Maske zu verlassen
- Zweiter Button **„globale Belegsuche“** über alle Belege
- **Wiedervorlage** pro Kunde (Datum + Notiz, ohne Mitarbeiterzuordnung)
- **Warnung** bei Überschreitung des Kreditlimits oder bei Kundensperre
- **Hinweisfeld** des Kunden wird beim Öffnen automatisch angezeigt, wenn etwas drinsteht
- Neuen Beleg anlegen (Angebot, Bestellung usw.)

**Positionserfassung**

| Feld | Beschreibung |
| --- | --- |
| Typ | links, Auswahl A/P/L/G, Standard A |
| Artikel | Suche über Artikelnummer oder Name |
| Menge | Stückzahl |
| Preis | Vorschlag aus VK-Stufe bzw. Sonderpreis, **frei änderbar**; Anzeige, was der Kunde **zuletzt** für den Artikel bezahlt hat, mit Datum |
| Übernehmen | per **Enter** oder Button, danach nächste Zeile oder Abschluss |

Positionsdetails (z. B. per Doppelklick): **Zusatztext**, **Seriennummern**, **Rabatt** pro Position.

**Abschluss (Detailfenster)**

- Zahlungsbedingung, Versandart
- Versandanschrift und Rechnungsanschrift (vorbelegt aus den Kundendaten)
- Versandkosten, Verpackung, Versicherung
- Beleg speichern, Nummer vergeben, drucken, als PDF/E-Rechnung mailen

**Nach Abschluss einer Rechnung**

- Beträge, Positionen und Versandkosten **gesperrt**
- Nur Infos wie Versandart/Zahlungsbedingung änderbar, mit Protokoll
- Korrektur ausschließlich über **Storno/Gutschrift** (GoBD)

**Belegfluss**

- Angebot → Bestellung → Lieferschein → Rechnung, jeweils **per Klick** übernehmen. **Angebot → Bestellung** und **Bestellung → Rechnung** (zweiter Knopf „Als Rechnung übernehmen“) **wandeln den Beleg um**: derselbe Datensatz, neue Belegart und neue Nummer; die alten Nummern stehen im **Verlauf** (Belegkopf „vormals Angebot AN…“), bleiben vergeben und sind über die Suche auffindbar. Bestellung → Lieferschein und Lieferschein → Rechnung ergeben einen neuen Beleg, „Kopieren als …“ ist eine echte Kopie – außer aus einer Bestellung in Lieferschein, Rechnung oder Proforma: dort **wandert** die übernommene Menge aus der Bestellung (Abschnitt 18, „Teilübernahme“)
- Angebot in eine **Lieferantenbestellung** kopieren (P-Artikel werden automatisch dem Lieferanten zugeordnet)
- **Proforma-Rechnung** (Vorkasse: Zahlung auf die Proforma, danach Umwandlung in die Rechnung, siehe Abschnitt 18)
- **Teillieferung:** Geliefertes wird berechnet und **wandert aus der Bestellung in den Folgebeleg**; die Bestellung behält nur den Rest (10 bestellt, 7 geliefert → in der Bestellung stehen 3). Ein unveränderlicher Verlauf zeigt am Beleg, was wann wohin ging („7 × … übernommen in RE…“)
- **Nachlieferschein:** aus der Rechnung kopieren, Menge anpassen (z. B. 1 von 2 nachliefern), **keine neue Rechnung**; das System **fragt, ob vom Lager abgebucht wird**
- **Kundenretoure** und **Kundengutschrift**
- **Anzahlungen/Teilzahlungen** auch auf Bestellungen (Vorkasse) buchbar; Bestellung und Rechnung zeigen bezahlten und offenen Restbetrag
- **Wiederkehrende Rechnungen** (Wartung, Hosting, Lizenzen)
- **Barverkauf** als normale Rechnung mit Zahlungsart „bar“
- **Kundenbestellungen per Excel/CSV importieren**, mit einer allgemeinen, festen Spaltenzuordnung

### 4.6 Einkaufsabwicklung

Aufbau **wie der Verkauf**, nur mit Lieferanten: Lieferantensuche, „Neuer Lieferant“, Belegsuche, Ansprechpartner pro Mandant, eigene Kundennummer beim Lieferanten pro Mandant (z. B. OKAY-Kundennummer bei API).

- Lieferantenbestellung mit Anzeige des **letzten EK** (pro Mandant) beim jeweiligen Lieferanten und des **aktuellen Preislistenpreises**
- **Wareneingang** mit Barcode-Scan per **Bluetooth-Scanner**, Teillieferungen/Rückstände
- **Lieferantenrechnung** nachträglich erfassen und mit dem Wareneingang verknüpfen, in **beide Richtungen** nachvollziehbar; Abgleich von Menge und Preis mit Warnung
- **E-Rechnungen empfangen:** ZUGFeRD/XRechnung einlesen und als Lieferantenrechnung anlegen
- **Rücksendung an Lieferanten** mit Gutschrift
- **RMA/Garantiefälle:** RMA-Nummer, Status bis Ersatz/Gutschrift, verknüpft mit der Werkstatt
- **WE-Rücksendung (Wunsch des Nutzers 18.11.2026):** gebuchte Ware aus einem Wareneingang geht an den Lieferanten zurück, wahlweise zur Gutschrift oder zum Austausch (z. B. 10 statt 5 geliefert, falsche Ware). Beleg mit eigener Nummer (WR), kein PDF. Beim Anlegen wird der Bestand abgebucht (nie unter 0), Seriennummern gehen auf „an Lieferant zurück“; die Lieferantenbestellung und die Lieferantenrechnung bleiben unverändert, der Ø-EK auch. Danach: versendet → Gutschrift erhalten (Gutschrift-Nr., Datum, Netto, USt, Brutto, Notiz; legt eine **Lieferanten-Gutschrift** an, siehe Abschnitt 18) oder Ersatz erhalten (legt einen Wareneingang an, der wie jeder gebucht wird und zu keiner Rechnung gehört) oder abgelehnt (Ware zurück ins Lager). Wird der Ersatz-Wareneingang ungebucht gelöscht, ist die Rücksendung wieder offen
- **Lieferantenbestellung mit Tabs (Wunsch des Nutzers 18.11.2026, wie die Kundenbestellung):** Positionen · Bedarf (Bestellvorschlag für diesen Lieferanten: Mindestbestand, Verkaufsverlauf, Fehlmenge aus Kundenbestellungen; nur Artikel, die der Lieferant laut Preisliste führt, umschaltbar auf alle; „Ausgewählte übernehmen“) · Versand & Konditionen (Liefertermin, Bezugnahme, Versandart und erwartete Nebenkosten – gehen in den ersten Wareneingang; dazu die Konditionen des Lieferanten) · Texte & Notizen (Text für den Lieferanten auf dem PDF, interne Notiz) · Wareneingänge
- **Wareneingang mit Tabs:** Positionen (Scan, Buchen) · Bestellungen (alle laufenden Bestellungen des Lieferanten wie „Lfd. Belege zu WE“: komplett übernehmen oder einzelne Positionen mit Teilmenge; Mengen in anderen offenen Wareneingängen sind abgezogen, Preislistenartikel nur mit Bestätigung) · Lieferschein & Kosten · Rücksendungen. Positionen aus mehreren Bestellungen in einem Wareneingang sind erlaubt; Buchen zieht den Status aller betroffenen Bestellungen nach, Löschen/Stornieren/Rest schließen sehen auch diese Wareneingänge
- **Seitenleiste Einkauf:** unter „Lieferant“ stehen wie beim Kunden „+ Bestellung“ und „+ Wareneingang“ (mit Auswahl der offenen Bestellung), sobald ein Lieferant, eine Bestellung, ein Wareneingang oder eine Rücksendung offen ist; „+ WE-Rücksendung“ (Auswahl des gebuchten Wareneingangs) steht seit 19.11.2026 ebenfalls auf der Lieferantenseite, der Menüpunkt WE-Rücksendung in der Seitenleiste entfällt (Rücksendungen stehen unter den Belegen des Lieferanten)
- **Bestellvorschlag:** fester Mindestbestand pro Artikel, sonst automatisch aus dem Verkaufsverlauf; per Klick Bestellung beim **günstigsten Lieferanten**. Die Fehlmenge aus offenen Kundenbestellungen kommt immer dazu, auch ohne Mindestbestand (4 bestellt, 2 am Lager, nichts unterwegs → 2; Wunsch des Nutzers 18.11.2026)

### 4.7 Lieferantenprodukte und Preise

- **Preislisten-Import** (CSV, FTP, Schnittstelle), automatische Aktualisierung von Preisen und Beständen per **Zeitplan**
- Verkaufspreisstufen:
  - **VK1** Endkunden
  - **VK2** gewerbliche Endkunden
  - **VK3** Händler
- VK als **Aufschlag auf den EK**: global, pro Kategorie, pro Artikel überschreibbar
- **Kundensonderpreise** pro Kunde und Artikel

**Importprofil je Lieferanten-Preisliste** (einmal einrichten, danach automatisch genutzt; Dateityp usw. wird **vorher** ausgewählt):

| Einstellung | Inhalt |
| --- | --- |
| Quelle | Datei-Upload, FTP/SFTP-Abruf oder Schnittstelle, mit Zeitplan |
| Dateityp | CSV, TXT, Excel (XLSX/XLS), XML |
| Format | Trennzeichen, Textbegrenzer, Zeichensatz (UTF-8, ANSI), Dezimaltrennzeichen, Kopfzeile ja/nein, erste Datenzeile |
| Feldzuordnung | Spalte der Datei → Feld: Lieferanten-Bestellnummer, Herstellernummer, EAN, Bezeichnung, Hersteller, Kategorie, EK netto, Bestand, Lieferzeit, Gewicht |
| Vorschau | erste Zeilen der Datei mit Zuordnung anzeigen, bevor importiert wird |
| Abgleich | Verknüpfung mit vorhandenen Artikeln über Herstellernummer, ersatzweise EAN |
| Protokoll | Anzahl neu, geändert, Fehler; fehlerhafte Zeilen einsehbar |

### 4.8 Bearbeitung – Finanzen

- **Offene Posten** Kunden und Lieferanten
- Zahlungseingänge und -ausgänge buchen, **Teilzahlungen**
- **Bankauszug-Import** (CAMT/CSV) mit automatischer Zuordnung zu offenen Rechnungen
- **SEPA-XML-Export** für Lieferantenzahlungen (StarMoney), mit **Skontofristen**
- **Kundenkontoauszug** über offene Rechnungen, einzeln und für alle Kunden
- **Mahnwesen:** Zahlungserinnerung, 1. und 2. Mahnung, Fristen und Gebühren einstellbar, Mahnungen drucken
- **Kassenbuch** gesperrt und protokolliert (TSE-Pflicht mit Steuerberater klären)
- **DATEV-Export**

Hinweis aus der Besprechung: Ein Kassenbuch in Excel ist bei einer Betriebsprüfung riskant, weil es nachträglich änderbar ist. Deshalb kommt das Kassenbuch gesperrt in die Software.

### 4.9 Bearbeitung – Lager

- **Bestandsänderungen:** Eigenverbrauch, Eigenbestand, Umbuchungen
- **Bestandshistorie** pro Artikel (Datum, Menge, Grund, Beleg)
- **Reservierung** für offene Bestellungen, automatisch (reserviert wird die Menge, die in der Bestellung noch steht, also der Rest nach einer Teilübernahme); Anzeige Gesamt / reserviert / verfügbar
- Nur der **verfügbare** Bestand geht an Shop und Marktplätze (Schutz vor Doppelverkauf)
- **Inventur:** Zählliste, gezählte Mengen eingeben, Differenzen automatisch buchen, pro Mandant. Artikelsuche (Nummer, Bezeichnung, EAN, Hersteller; mehrere Wörter) und Kategorie samt Unterkategorien als Live-Filter; angezeigt werden nur aktive Artikel, der Haken „Nicht aktiv“ zeigt stattdessen nur die nicht aktiven; eingetragene Mengen bleiben beim Suchen, Filtern und Neuladen erhalten und werden gemeinsam gebucht, auch wenn der Artikel gerade nicht angezeigt wird. Enter im Zählfeld springt zurück in die Suche; doppeltes Absenden bucht nur einmal
- **Artikel & Bestand:** Liste mit Suche, Kategorie, „nur mit Bestand“ und Haken „Nicht aktiv“ (Standard: nur aktive Artikel; mit Haken nur die als nicht aktiv markierten)
- **Lagerbewertung** mit dem maßgebenden Import-EK (Wareneingang ändert den EK nicht)
- **Lagerplätze** (Regal/Fach)
- **Seriennummern** durchgängig vom Wareneingang bis zur Werkstatt

### 4.10 Stammdaten

- Kunden (Kundenart, VK-Stufe, Kreditlimit pro Mandant, Sperre, Newsletter-Einwilligung pro Firma, USt-ID mit EU-Prüfung)
- Lieferanten
- Artikel, Leistungen, Preislisten
- Kategorien **mehrstufig** (z. B. Prozessoren > Intel/AMD > Sockel/Serie)

Die vollständigen Feldlisten stehen in [Abschnitt 5](#5-feldlisten-stammdaten).

### 4.11 ESD-Key-Verwaltung (Software-Lizenzen)

- Key-Import, Bestand **pro Mandant**
- Beim Verkauf automatisch dem **Beleg zugeordnet**, Versand per Mail oder auf der Rechnung
- **Volumenkeys** (z. B. Office-Volumenlizenzen) mit maximaler Vergabeanzahl (z. B. 10), danach gesperrt
- Denselben Key **nicht zweimal an denselben Kunden**, außer der Verkäufer entscheidet es
- Bei **Retoure** individuelle Entscheidung: Key wieder freigeben oder sperren

### 4.12 Werkstatt

- Geräteannahme (PC, Laptop) mit Seriennummer und Fehlerbeschreibung
- **Abholschein** für den Kunden
- Status: angenommen → in Reparatur → fertig, mit **Statusmail** an den Kunden
- Teile und Arbeitszeit erfassen, Abrechnung als Rechnung oder Garantie
- Übergang in die **Lieferanten-RMA**
- Pro Mandant

### 4.13 Versand

- Zuerst **DHL Paket** und **Deutsche Post** (meistgenutzt), später **GLS, DPD, UPS**
- Etiketten direkt aus dem Lieferschein
- Sendungsnummer am Beleg und in der Kundenmail
- Zugangsdaten der Versanddienste in den Voreinstellungen

### 4.14 Schnittstellen

- **Shopware 6:** mehrere Shops, je einem Mandanten zugeordnet (okaycomputer.de, vienola.de bzw. zone-edv.de); Bestellimport, Bestands- und Preisabgleich, Kategorien
- **eBay** und **Kaufland**
- **Amazon wird nicht benötigt**
- Nur der verfügbare Bestand wird gemeldet
- **Newsletter** mit Double-Opt-in und Abmeldelink

### 4.15 E-Rechnung und Steuern

- Ausgang: **ZUGFeRD** (PDF mit XML), optional **XRechnung**
- Eingang: ZUGFeRD/XRechnung einlesen
- **Reverse Charge** (EU) mit USt-ID-Prüfung
- Optional **Differenzbesteuerung**

### 4.16 Auswertungen und Startseite

- Umsatz, Marge, Topseller **pro Mandant und Zeitraum**, Excel-Export
- Startseite: fällige Wiedervorlagen, offene Bestellungen, fällige Mahnungen

### 4.17 Voreinstellungen

- Mandanten mit Firmendaten und **Briefbogen**
- Nummernkreise, Steuersätze
- **Versandarten** mit Standardkosten
- **Zahlungsbedingungen** (z. B. Vorkasse, 14 Tage 2 % Skonto)
- Mahnstufen, Fristen, Gebühren
- E-Mail-Server, **Mailvorlagen**, **Textbausteine**
- Importformat für Kundenbestellungen
- Zugangsdaten Versanddienste (Etikettenerstellung)
- Zugangsdaten Shops und Marktplätze
- Globale Aufschläge VK1–VK3
- Benutzer und Rechte

### 4.18 Datenschutz

- Kundendaten auf Anfrage **sperren oder anonymisieren**
- Rechnungen bleiben wegen der **Aufbewahrungspflicht** erhalten
- **Belegarchiv mit Anhängen**

## 5. Feldlisten Stammdaten

### 5.1 Artikelstamm

| Bereich | Felder |
| --- | --- |
| Grunddaten | Bezeichnung, **erweiterte Bezeichnung** (zugleich Langbeschreibung im Shop, keine separate Langbeschreibung), Kategorie, Hersteller, Herstellernummer, EAN, Checkbox „nicht aktiv“, Garantie in Monaten |
| Maße | Länge, Breite, Höhe, Gewicht in kg |
| Preise | letzter EK **pro Mandant**, EK netto (Import-EK maßgebend), VK1–VK3 mit Aufschlag, Steuersatz |
| Online | bis zu **10 Bilder** (erstes = Hauptbild, gehen an Shop/eBay/Kaufland), **eine** Checkbox „online“ für alle Kanäle, Kurzbeschreibung, Keywords, Metatitel, Meta-Description, Meta-Keywords |
| Lieferanten | Liste je Lieferant: Preis, Bestand, aus welcher Preisliste, **Bestellnummer des Lieferanten**; Verknüpfung über die **Herstellernummer**; die Bestellnummer wird automatisch in die Lieferantenbestellung übernommen |

### 5.2 Kundenstamm

| Bereich | Felder |
| --- | --- |
| Grunddaten | Kundennummer, Name 1, Name 2, Name 3, Straße, PLZ, Ort, Land, Webseite, Gesellschaftsform, Inhaber bzw. Geschäftsführer |
| Einstufung | **Kundenart** (Endkunde, gewerblicher Endkunde, Wiederverkäufer) und **Preisstufe** VK1–VK3 – **getrennt** voneinander einstellbar |
| Automatisch | Datum **Erstkontakt** wird beim Anlegen gesetzt; **letzte Änderung** mit Datum **und Benutzer** |
| Hinweis | Hinweisfeld; wird beim Öffnen des Kunden **automatisch angezeigt**, wenn etwas drinsteht |
| Vorgaben für Belege | **Standard-Zahlungsbedingung** (wird bei jedem neuen Beleg vorausgewählt, im Beleg änderbar), abweichende **Rechnungsanschrift**, **mehrere Versandanschriften**, eine davon als **Standard** markierbar |
| Ansprechpartner | mehrere: Name, **Abteilung/Position**, E-Mail, Telefon, **Durchwahl**, Mobil, eigener **Onlineshop-Zugang** pro Ansprechpartner |
| Finanzen | **USt-ID**, **Kreditlimit** (pro Mandant), **Bankverbindung**, **SEPA-Mandat** mit Mandatsreferenz und Datum |

Onlineshop-Zugang: Die Warenwirtschaft legt das Konto direkt in Shopware an und schickt dem Kunden einen Link, mit dem er sein Passwort selbst setzt. Passwörter werden **nie im Klartext** gespeichert oder angezeigt.

### 5.3 Lieferantenstamm

Aufbau **wie der Kundenstamm** (Name 1–3, Straße, PLZ, Ort, Ansprechpartner, Bankverbindung, USt-ID, Zahlungsbedingungen), zusätzlich:

| Bereich | Felder |
| --- | --- |
| Pro Mandant | eigene Kundennummer beim Lieferanten, eigene Ansprechpartner |
| Konditionen | **Zahlungsziele mit Skonto** (wichtig für den SEPA-Export), Mindestbestellwert, Lieferzeit, versandkostenfrei ab Betrag |

## 6. Prüfdurchgänge und Ergänzungen

Das Pflichtenheft wurde in mehreren Runden auf Lücken geprüft. Alle Vorschläge wurden übernommen.

| Runde | Übernommene Ergänzungen |
| --- | --- |
| 1 | offene Posten pro Mandant, Nummernkreise, Benutzer/Login, GoBD/DATEV, ZUGFeRD, automatische Umwandlung P → A, Seriennummern, Shopware-Anbindung, Steuerfälle, Startseite, Backup |
| 2 | Kasse, Teillieferung/Rückstand, Reservierung, Bestellvorschlag, Inventur, Lieferanten-RMA, Anzahlungen, Barcode-Scanner, Auswertungen, DSGVO |
| 3 | E-Rechnungsempfang, Belegarchiv mit Anhängen, Lagerbewertung, Skonto im Zahlungsausgang, Kreditlimit/Kundensperre, Textbausteine und Mailvorlagen |
| 4 | wiederkehrende Rechnungen, USt-ID-Prüfung (EU), Kundensonderpreise, Dublettenprüfung, Lagerplätze, Proforma-Rechnung |
| Konsistenz | Kreditlimit, letzter EK, Bestellvorschlag, ESD-Key-Bestand, G-Artikel-Bestand, Werkstatt und Kassenbuch jeweils **pro Mandant**; Newsletter-Einwilligung **pro Firma** |

Nach Erstellung des Pflichtenhefts wurden ergänzt: Feldlisten Artikel, Kunden, Lieferanten (Abschnitt 5) und das Importprofil für Preislisten (Abschnitt 4.7).

## 7. Offene Klärungen

| Thema | Mit wem / Was |
| --- | --- |
| TSE-Pflicht beim Barverkauf | Steuerberater |
| Gemeinsame Kundendaten zweier Firmen | Datenschutzberater (Vereinbarung über gemeinsame Verantwortung) |
| Importprofil für mehrere Lieferanten mit gleichem Format nutzbar? | Frage noch nicht beantwortet |
| Excel-Vorlage für das ZUGFeRD-Skript | Vorlage an Claude schicken |
| Umsetzungsreihenfolge der Module | grob festgelegt in Abschnitt 16, Details offen |
| Lastschrift-Einzug (SEPA pain.008) bei Zahlungsart „Lastschrift“ bauen? Gläubiger-ID für OKAY und ZONE eintragen? | Entscheidung des Nutzers steht aus |
| Rechnung RE2026-00003 wurde ohne Lagerabbuchung abgeschlossen (Fehler inzwischen behoben) | per Storno/Gutschrift korrigieren oder Bestand für Artikel 24007 nachbuchen |

## 8. Technik und Aufbau des Codes

### 8.1 Technik

| Bereich | Wahl | Grund |
| --- | --- | --- |
| Sprache | Python **3.12** | wwok läuft in venv312; System-Python des Servers bleibt 3.10.12 |
| Framework | Django 5.2 (LTS) | Benutzer, Rechte, Formulare, Migrationen und Verwaltungsoberfläche schon eingebaut |
| Datenbank | MySQL/MariaDB auf dem Server, SQLite lokal | MySQL läuft schon auf dem Server |
| Webserver | Gunicorn hinter nginx (Plesk) | Standard für Django |
| Einstellungen | `.env`-Datei | Passwörter nie im Code |

### 8.2 Ordnerstruktur

```
wwok/
├── apps/
│   ├── kern/          Mandanten, Basisklassen, Änderungsprotokoll (GoBD), Mail-Einstellungen je Mandant
│   ├── stammdaten/    Kunden, Lieferanten, Artikel, Preislistenartikel (Typ P), Preise, Dialog zum Bearbeiten
│   ├── verkauf/       Belege (Angebot bis Gutschrift), Positionen, Gruppen, PDF, Mailversand
│   ├── einkauf/       Bestellungen, Wareneingang, Lieferantenrechnungen, RMA, Preislisten-Import, SEPA
│   ├── lager/         Lagerbewegungen, Bestand, Reservierung, Inventur, Seriennummern
│   ├── finanzen/      Offene Posten, Zahlungen, Mahnwesen, Bankimport, Kassenbuch, DATEV
│   ├── werkstatt/     Reparaturen mit Abholschein und Abrechnung
│   ├── esd/           Lizenzschlüssel (Key-Bestand, Vergabe)
│   └── shop/          Shopware-6-Anbindung (Bestellungen, Bestand, Preise)
├── config/            Django-Einstellungen, URLs
├── deploy/            Anleitung für Plesk / Server
├── docs/              Pflichtenheft
├── medien/            hochgeladene Dateien (nicht im Repository)
├── static/            CSS, JS, Bilder
├── templates/         HTML-Vorlagen (base.html mit Navigation, Dialog und Registerkarten)
├── .env.example       Vorlage für Zugangsdaten
├── .gitignore
├── manage.py
└── requirements.txt
```

Der Versand (DHL-Paket-Label, Deutsche Post INTERNETMARKE) liegt in `apps/verkauf` (`dhl.py`, `internetmarke.py`, `versand.py`), die Übersicht mit Umsatz und Top-Listen ist das Dashboard auf der Startseite. Noch nicht umgesetzt: eBay/Kaufland, Auswertungen mit Marge und Excel, Anmeldung mit 2FA (siehe Abschnitt 16).

### 8.3 Was bereits programmiert ist

**`apps/kern`**

| Teil | Funktion |
| --- | --- |
| `Zeitstempel` | Basisklasse: angelegt am, letzte Änderung, **geändert von** (Benutzer wird automatisch eingetragen) |
| `Mandant` | Firma mit Kurzname, Firmenname, Adresse, USt-ID, Steuernummer, Telefon, E-Mail, IBAN, BIC, Gläubiger-ID (SEPA) |
| `AenderungsProtokoll` | jede Änderung an Stammdaten mit Zeitpunkt, Benutzer, Tabelle, Datensatz, Aktion, Werten **vorher und nachher**; Einträge können **nicht geändert oder gelöscht** werden |
| `middleware.py` | merkt sich den angemeldeten Benutzer pro Anfrage |
| `signals.py` | schreibt das Protokoll automatisch bei Anlegen, Ändern, Löschen |

**`apps/stammdaten`**

| Modell | Inhalt |
| --- | --- |
| `Zahlungsbedingung` | Zahlungsziel, Skontotage, Skonto %, Vorkasse, Lastschrift, Text auf Beleg |
| `Steuersatz` | Bezeichnung, Prozent |
| `Kategorie` | mehrstufig, mit Aufschlägen VK1–VK3 pro Kategorie |
| `Hersteller` | Name |
| `Kunde` | alle Felder aus 5.2, Kundenart und Preisstufe getrennt, Erstkontakt automatisch, SEPA-Mandat |
| `KundeMandant` | pro Mandant: Kreditlimit, Newsletter-Einwilligung mit Double-Opt-in-Datum |
| `Kundenanschrift` | Rechnungs- und mehrere Versandanschriften; nur **eine** Standardanschrift je Art (beim Setzen einer neuen wird die alte automatisch zurückgesetzt) |
| `Ansprechpartner` | für Kunde **oder** Lieferant; bei Lieferanten mit Mandant; Abteilung, Durchwahl, Mobil, Hauptkontakt, Onlineshop-Zugang, Shopware-Kunden-ID |
| `Lieferant` | Felder wie Kunde plus Mindestbestellwert, versandkostenfrei ab, Lieferzeit |
| `LieferantMandant` | pro Mandant: unsere Kundennummer beim Lieferanten |
| `Artikel` | alle Felder aus 5.1, Typ A/P/L/G |
| `ArtikelMandant` | pro Mandant: letzter EK mit Datum |
| `ArtikelBild` | bis zu 10 Bilder, Position 1 = Hauptbild |
| `ArtikelLieferant` | Lieferant, Bestellnummer, EK, Bestand, Preisliste, Einlesedatum |

Die Pflege läuft zusätzlich über die Django-Verwaltungsoberfläche unter **`/verwaltung/`**. Kunden und Lieferanten werden im Alltag in einem **Dialog mit Registerkarten** bearbeitet (Kunde: Grunddaten, Einstufung & Hinweis, Steuer & Bank, Ansprechpartner, Anschriften, Mandant; Lieferant: Grunddaten, Konditionen, Bank, Ansprechpartner, Mandant). Der Dialog hat feste Größe und Position, nur der Inhalt scrollt. Mit ungespeicherten Eingaben schließen Klick daneben, Escape, × und „Abbrechen“ den Dialog nur nach Rückfrage („Weiter bearbeiten“ / „Verwerfen“); Text markieren und außerhalb loslassen schließt nicht.

**Weitere Module (alle produktiv)**

| App | Inhalt |
| --- | --- |
| `verkauf` | Startseite mit Kundensuche und offenen Belegen; Kundenkarte mit Belegliste (Filter, Seiten); Angebot → Bestellung → Lieferschein → Rechnung per Klick; **Beleg kopieren als …** (beliebige Belegart, Angebot wird auf Wunsch abgeschlossen); Sammelrechnung aus mehreren Lieferscheinen; Gruppen (G) mit Unterpositionen; Teillieferung; Gutschrift/Storno; PDF mit Seriennummern; Mailversand je Mandant mit Protokoll |
| `lager` | Bestand je Mandant, unveränderliche Lagerbewegungen, Reservierung, Inventur, Seriennummern, Retoure; Bestand darf nie unter 0 fallen |
| `finanzen` | Offene Posten, Zahlungen und Stornos mit Grund, Kontoauszug, Mahnwesen mit PDF/Mail, Bankimport (CAMT.053/CSV), Kassenbuch (gesperrt), DATEV-Export |
| `einkauf` | Lieferantenbestellungen (Suche über Lieferanten- und eigene Artikel), Wareneingang mit Scan, Lieferantenrechnungen (auch ZUGFeRD/XRechnung), SEPA-Export (pain.001), RMA, Bestellvorschlag, **Preislisten-Import** (FTP/SFTP/URL, Zeitplan, Assistent), Lieferantenliste mit Dialog |
| `werkstatt` | Geräteannahme, Abholschein, Status, Teile und Arbeit, Abrechnung, Übergang in RMA |
| `esd` | Key-Import, automatische Vergabe beim Verkauf (nur Kategorie Software), Volumenkeys, Retoure |
| `shop` | Shopware 6 über die Admin-API: Bestellimport, Bestands- und Preisabgleich je Shop, Artikeldaten (Texte, Maße, Hersteller, Kategorie, Bilder, Bestand, Preise, neue Artikel) per Knopf oder im eingestellten Takt |

**Getestet:** Die Testsuite (Abschnitt 19) deckt Modelle, Belegfluss, Lager- und Bestandsregeln, Preislisten-Import, Zahlungen, ESD, Shop und die Dialoge für Kunden/Lieferanten ab (aktuell rund 1.560 Tests).

### 8.4 Sicherheit in den Einstellungen

- Im Echtbetrieb (`DJANGO_DEBUG=false`): HTTPS-Umleitung, sichere Cookies, HSTS (nur für die Subdomain, nicht für die Shops), kein Einbetten in fremde Seiten
- Passwörter mindestens **12 Zeichen**
- Sitzung endet nach **10 Stunden** oder beim Schließen des Browsers

### 8.5 Konfiguration über `.env`

Grundwerte stehen in `.env.example`. Zusätzlich liest das Programm Geheimnisse aus der `.env` auf dem Server, nie aus dem Repository. In der Datenbank stehen Geheimnisse nie im Klartext.

**Ausnahme Mailversand:** Das Passwort des Mail-Postfachs wird je Mandant in der Verwaltung eingetragen (Verwaltung → Mandanten → E-Mail-Versand). Es wird verschlüsselt gespeichert (Schlüssel abgeleitet aus `DJANGO_SECRET_KEY`), nie wieder angezeigt und steht nicht im Änderungsprotokoll. „Testmail senden“ prüft den Versand. Wird `DJANGO_SECRET_KEY` geändert, muss das Passwort neu eingetragen werden.

| Variable | Zweck |
| --- | --- |
| `SMTP_PASSWORD_<MANDANT>` | Nur noch Rückfall: Passwort des Mail-Postfachs je Mandant (z. B. `SMTP_PASSWORD_OKAY`), wenn in der Verwaltung keines hinterlegt ist |
| `PREISLISTE_PW_<ID>` | Passwort für den Abruf einer Lieferanten-Preisliste (`<ID>` = Nummer des Importprofils, z. B. `PREISLISTE_PW_1`) |
| `SHOP_SECRET_<ID>` | Zugangsschlüssel der Shopware-API je Shop |

Beim Prüfen der `.env` nur die **Variablennamen** anzeigen (`cut -d= -f1`), nie die Werte.

## 9. Server und Subdomain

### 9.1 Ausgangslage

- **Strato V-Server** mit **Plesk**, Ubuntu 22.04, Python 3.12 für wwok (venv312, über deadsnakes installiert), System-Python 3.10.12
- Shops liegen unter `/var/www/vhosts/okaycomputer.de` und `/var/www/vhosts/zone-edv.de`

### 9.2 Empfehlung und Begründung

- Eigene Subdomain **`erp.okaycomputer.de`**
- Eigenes Verzeichnis **neben** dem Shop, nicht als Unterordner im Shop und nicht in `httpdocs`:
  `/var/www/vhosts/okaycomputer.de/erp.okaycomputer.de`
- Grund: Die Warenwirtschaft gehört zu beiden Firmen, muss aber vom Shop getrennt sein. Bei einer Sicherheitslücke in Shopware darf niemand an Kundendaten, Rechnungen und Bankdaten kommen.
- Alternative: eine neutrale eigene Domain, falls `okaycomputer.de` wegen ZONE Handel unpassend wirkt. Funktional kein Unterschied.

| Punkt | Empfehlung |
| --- | --- |
| Datenbank | eigene MySQL-Datenbank `wwok` mit eigenem Benutzer, **nicht** die Shop-Datenbank |
| Zugriff | Login mit 2FA, wenn möglich zusätzlich nur aus dem Büro-Netz oder per VPN |
| SSL | eigenes Let's-Encrypt-Zertifikat, nur HTTPS |
| Systembenutzer | wenn möglich eigenes Plesk-Abonnement mit eigenem Systembenutzer, damit Shop und Warenwirtschaft auch auf Dateiebene getrennt sind |

### 9.3 Zielstruktur auf dem Server

```
/var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/
├── app/        ← Git-Checkout von akuvvet/wwok
├── public/     ← Dokumentenstamm (nur static/ und medien/)
└── venv/       ← Python-Umgebung
```

## 10. GitHub

### 10.1 Warum GitHub

- Saubere **Versionsgeschichte**: jede Änderung nachvollziehbar, kaputte Version schnell zurückdrehen
- Code ist gleichzeitig **gesichert**
- Server holt Updates per `git pull`

### 10.2 Namensfindung

| Schritt | Name |
| --- | --- |
| erster Vorschlag von Claude | `badmon61/warenwirtschaft` |
| Idee des Nutzers | `kuvvet/wwkvt` |
| Änderung | Projektname **`wwok`** statt `wwkvt` |
| endgültig | **`akuvvet/wwok`** |

Hinweis: Der Kurzname ist für Fremde nicht selbsterklärend. Deshalb in der Repo-Beschreibung auf GitHub eintragen: „Web-Warenwirtschaft für OKAY Computer und ZONE Handel“. Umbenennen ist bei GitHub später jederzeit möglich, alte Links werden weitergeleitet.

### 10.3 Regeln

| Punkt | Regel |
| --- | --- |
| Sichtbarkeit | **privat** (Geschäftslogik und Aufbau der Kundendaten) |
| Zugangsdaten | nie ins Repo, nur in `.env` auf dem Server; `.env` steht in `.gitignore` |
| Branches | `main` = Stand auf dem Server, Entwicklung in eigenen Branches |
| Deployment | auf dem Server per `git pull` |
| Datenbank | Tabellenstruktur als Migrationen im Repo, echte Daten nie |
| Dokumentation | Pflichtenheft als Markdown im Repo (`docs/pflichtenheft.md`) |

## 11. Lokal starten (Windows)

In **Git Bash** (oder PowerShell mit angepassten Pfaden):

```bash
cd /d/Desktop/github/wwok
py -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env
```

In der `.env` für lokal eintragen:

```
DJANGO_DEBUG=true
DB_ENGINE=sqlite
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
```

Dann:

```bash
py manage.py migrate
py manage.py createsuperuser
py manage.py runserver
```

Aufrufen: <http://127.0.0.1:8000/verwaltung/>

`mysqlclient` wird lokal nur gebraucht, wenn auch lokal MySQL genutzt wird; bei Installationsproblemen unter Windows die Zeile `mysqlclient` in `requirements.txt` beim lokalen Installieren auslassen.

Tests starten (vor jedem Commit):

```bash
python manage.py test apps
```

Bei `DJANGO_DEBUG=false` leitet die Anwendung auf HTTPS um; lokal deshalb `DJANGO_DEBUG=true` setzen.

### Testumgebung für Claude

`.claude/launch.json` startet über `scripts/testumgebung.py` die lokale Instanz auf http://127.0.0.1:8000: Python aus `.venv`, Datenbank `lokal.sqlite3`, ausstehende Migrationen werden eingespielt. Der Testzugang `claude_test` wird bei jedem Start neu gesetzt und steht in `.lokal_testzugang` (nicht im Repository). Mit `/auftrag <Text>` setzt Claude einen Text um, prüft ihn dort und committet; Push und Deploy folgen erst nach Freigabe.

## 12. Erstes Hochladen zu GitHub – so ist es gelaufen

| Schritt | Ergebnis | Lösung |
| --- | --- | --- |
| Grundgerüst als ZIP nach `D:\Desktop\github` entpackt | ✔ | – |
| `git` in **PowerShell** | Fehler: „git wurde nicht als Name eines Cmdlet … erkannt“ | Git ist installiert (2.47.1), aber nur in **Git Bash** verfügbar → in Git Bash arbeiten. Alternativ in PowerShell: `winget install --id Git.Git -e`, PowerShell neu öffnen |
| Git Bash zeigte `~ (main)` im Benutzerordner | der Benutzerordner `C:\Users\akuvvet` war versehentlich ein leeres Git-Repository („does not have any commits yet“) | `cd ~ && mv .git .git_unbenutzt` – umbenannt statt gelöscht, später im Explorer löschbar |
| `git init`, `git add`, `git commit` im Projektordner | ✔ Commit `2cdbf62` „Grundgerüst wwok: Kern, Stammdaten, Pflichtenheft“ | – |
| `git push` | Fehler: „Repository not found“ (auch nach erneuter Anmeldung) | Repo auf **github.com/new** angelegt: Owner `akuvvet`, Name `wwok`, **Private**, ohne README/.gitignore/Lizenz |
| `git push -u origin main` | ✔ 34 Objekte hochgeladen, Branch `main` verbunden | – |

Hilfreiche Befehle aus der Fehlersuche:

```bash
git remote -v
git log --oneline
# gespeicherte GitHub-Anmeldung löschen, danach neu anmelden:
printf "protocol=https\nhost=github.com\n\n" | git credential reject
# prüfen, mit welchem Konto Git angemeldet ist:
printf "protocol=https\nhost=github.com\n\n" | git credential fill | grep username
```

Einmalig Name und E-Mail für Commits setzen (falls verlangt):

```bash
git config --global user.name "Dein Name"
git config --global user.email "deine-adresse"
```

Wenn die E-Mail auf GitHub nicht sichtbar sein soll: die `noreply`-Adresse aus GitHub → Settings → Emails nehmen.

## 13. Server-Einrichtung Schritt für Schritt

### 13.1 Plesk

| Wo | Was | Stand |
| --- | --- | --- |
| Websites & Domains → okaycomputer.de → Subdomain hinzufügen | Subdomain `erp` | ✔ erledigt |
| Subdomain → Hosting-Einstellungen | Dokumentenstamm `erp.okaycomputer.de/public`, Systembenutzer notieren | offen |
| SSL/TLS-Zertifikate | Let's Encrypt, „HTTP auf HTTPS umleiten“ an | offen |
| Datenbanken → Datenbank hinzufügen | Name `wwok`, neuer Benutzer `wwok`, starkes Passwort notieren | offen |
| Apache & nginx-Einstellungen | **Proxy-Modus aus**, sonst Fehler bei `location /` | offen |

### 13.2 Pakete (SSH als root)

```bash
python3 --version        # System-Python 3.10.12 bleibt unverändert (Plesk und andere Seiten nutzen es)
# wwok läuft seit 15.11.2026 mit Python 3.12 in einer eigenen Umgebung (venv312, „venv“ ist eine Verknüpfung darauf):
# add-apt-repository -y ppa:deadsnakes/ppa && apt-get install -y python3.12 python3.12-venv python3.12-dev
apt install -y python3-venv python3-dev default-libmysqlclient-dev pkg-config build-essential git
```

### 13.3 Deploy-Key für das private Repo

```bash
ssh-keygen -t ed25519 -f /root/.ssh/wwok_deploy -N "" -C "wwok-server"
cat >> /root/.ssh/config <<'EOF'
Host github-wwok
    HostName github.com
    User git
    IdentityFile /root/.ssh/wwok_deploy
EOF
cat /root/.ssh/wwok_deploy.pub
```

Die angezeigte Zeile (beginnt mit `ssh-ed25519`) auf GitHub eintragen: **akuvvet/wwok → Settings → Deploy keys → Add deploy key**, Titel z. B. `strato-server`, **ohne** Schreibrecht.

### 13.4 Code holen und einrichten

```bash
cd /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de
git clone github-wwok:akuvvet/wwok.git app     # beim ersten Mal mit "yes" bestätigen
python3 -m venv venv
source venv/bin/activate
pip install -r app/requirements.txt
cp app/.env.example app/.env
python -c "import secrets; print(secrets.token_urlsafe(50))"
nano app/.env
```

In der `.env` eintragen:

```
DJANGO_SECRET_KEY=<erzeugter Schlüssel>
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=erp.okaycomputer.de
DB_ENGINE=mysql
DB_NAME=wwok
DB_USER=wwok
DB_PASSWORD=<Passwort aus Plesk>
DB_HOST=localhost
DB_PORT=3306
MEDIA_ROOT=/var/www/vhosts/okaycomputer.de/erp-daten/medien
```

Dann:

```bash
cd app
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

### 13.5 Gunicorn als Dienst

Datei `/etc/systemd/system/wwok.service`:

```ini
[Unit]
Description=wwok Warenwirtschaft
After=network.target

[Service]
User=SYSTEMBENUTZER_DER_SUBDOMAIN
WorkingDirectory=/var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app
ExecStart=/var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8010 --workers 3
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now wwok
```

`SYSTEMBENUTZER_DER_SUBDOMAIN` steht in Plesk unter Hosting-Einstellungen, Feld „Benutzername“.

### 13.6 nginx-Weiterleitung

Plesk → erp.okaycomputer.de → Apache & nginx-Einstellungen → **Zusätzliche nginx-Anweisungen**:

```nginx
location /static/ { alias /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app/staticfiles/; }
location /medien/ { alias /var/www/vhosts/okaycomputer.de/erp-daten/medien/; }
location / {
    proxy_pass http://127.0.0.1:8010;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Optional: Zugriff nur aus dem Büro-Netz (`allow BÜRO-IP; deny all;`).

### 13.7 Rechte

```bash
chown -R SYSTEMBENUTZER:psacln /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/venv
chmod 600 /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app/.env
```

### 13.8 Aktualisieren nach Änderungen

Der Systembenutzer der Subdomain ist `shopokaycomputer`. Ablauf nach jedem Commit auf `main`:

```bash
cd /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app
git pull
chown -R shopokaycomputer:psacln .
chmod 600 .env
sudo -u shopokaycomputer ../venv/bin/pip install -r requirements.txt   # nur wenn sich requirements.txt geändert hat
sudo -u shopokaycomputer ../venv/bin/python manage.py migrate --noinput   # nur bei neuen Migrationen nötig
sudo -u shopokaycomputer ../venv/bin/python manage.py collectstatic --noinput
systemctl restart wwok
systemctl is-active wwok
```

Der Python-Pfad ist `../venv/bin/python` (außerhalb von `app/`).

### 13.9 Sicherung

Plesk-Backup **täglich** für die Subdomain **und** die Datenbank `wwok`, zusätzlich extern ablegen.

### 13.10 Zeitpläne (Cron)

Datei `/etc/cron.d/wwok`:

```
*/10 * * * * shopokaycomputer cd /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app && ../venv/bin/python manage.py shop_sync >> /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/sync.log 2>&1
7 * * * * shopokaycomputer cd /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app && ../venv/bin/python manage.py preislisten_abrufen >> /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/sync.log 2>&1
* * * * * shopokaycomputer cd /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app && ../venv/bin/python manage.py belege_verwerfen --ausfuehren >> /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/sync.log 2>&1
30 5 * * * shopokaycomputer cd /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/app && ../venv/bin/python manage.py wiederkehrende_rechnungen >> /var/www/vhosts/okaycomputer.de/erp.okaycomputer.de/sync.log 2>&1
```

`shop_sync` und `preislisten_abrufen` laufen nur, wenn Shops bzw. Preislisten-Profile mit Zeitplan eingerichtet sind. `belege_verwerfen` (jede Minute) verwirft nicht gespeicherte (vorläufige) Belege, deren Maske geschlossen wurde (ohne Lebenszeichen nach 60 Minuten) (ohne `--ausfuehren` nur Probelauf, `--mandant` begrenzt); `wiederkehrende_rechnungen` legt zu fälligen Abos die Bestellung an.

Der Befehl `preislisten_abrufen` übernimmt danach auch den günstigsten Lieferanten-EK in die A-Artikel, sofern es der Zeitplan verlangt: Verwaltung → „Zeitplan: Günstigster EK in Artikel übernehmen“ (aus / nach jedem Preislisten-Abruf / täglich zur Uhrzeit / wöchentlich mit Wochentag und Uhrzeit). Der stündliche Cron-Aufruf genügt dafür.

## 14. Weiterarbeiten mit Claude

- Das Repo `akuvvet/wwok` kann in einer Claude-Sitzung (Code-Bereich) verbunden werden. Lesen, Ändern und Testen funktioniert.
- **Pushen** geht erst, wenn die **Claude-GitHub-App** für das Konto installiert ist:
  1. https://github.com/apps/claude/installations/select_target öffnen
  2. Konto `akuvvet` wählen
  3. „Only select repositories“ → `wwok` → speichern
- Befehle auf dem Server führt Claude **nicht** selbst aus. Die SSH-Befehle werden selbst ausgeführt und die Ausgabe an Claude geschickt.
- Zum Start einer neuen Sitzung: diese README und `docs/pflichtenheft.md` lesen lassen und mit dem nächsten offenen Punkt aus Abschnitt 15 weitermachen.
- Antworten bitte auf Deutsch, kurz und mit kopierfertigen Befehlen.

## 15. Aktueller Stand

**Planung**

- [x] Anforderungen gesammelt und in vier Prüfrunden ergänzt
- [x] Pflichtenheft erstellt (18 Abschnitte, Feldlisten, Importprofil)
- [ ] Offene Klärungen aus Abschnitt 7

**Code (laufend auf dem Server)**

- [x] Grundgerüst, Einstellungen über `.env`
- [x] Mandanten, Änderungsprotokoll (unveränderlich), letzte Änderung mit Benutzer
- [x] Stammdaten Kunde, Lieferant, Artikel laut Feldlisten; Importe aus den Mention-Tabellen (Abschnitt 19)
- [x] Eigene Masken: Kundensuche, Kundenkarte, Belegmaske, Dialog für Kunden/Lieferanten mit Registerkarten
- [x] Verkaufsabwicklung: Belegfluss, Gruppen (G), Sammelrechnung, Teillieferung, Kopieren, Gutschrift, PDF, Mailversand
- [x] Übernehmen wandelt um (Angebot → Bestellung, Bestellung → Rechnung): Modell `BelegUmwandlung` (Verlauf mit alter Art, Nummer, Datum, Benutzer), Hinweis „vormals …“ im Belegkopf, Suche nach alten Nummern, PDF-Bezug aus dem Verlauf
- [x] Gutschrift: Abfrage und Spalte B (Reparaturbestand, Lagerbestand, keine Ware), Buchung beim Abschließen; Reparaturbestand am Artikel mit Umbuchen und Ausbuchen
- [x] Leistungsartikel: eigene Kategorien, Kosten als EK, Anlegen und Import (CSV, Excel) in der Verwaltung
- [x] Lieferantenrechnung löschen mit Pflichtnotiz, Zuordnung zum Wareneingang wird aufgehoben
- [x] Beleg: Kopf und Symbole bleiben beim Scrollen stehen, die Positionsliste zeigt 6 Positionen und scrollt danach; unter „Neuer Beleg“ nur noch Angebot und Bestellung, Rechnung und Proforma entstehen per Übernehmen oder Kopieren, Gutschrift nur aus einer Rechnung
- [x] Belegart Auftrag heißt überall Bestellung (Kundenbestellung im Verkauf); Lieferantenbestellungen im Einkauf und Werkstattaufträge bleiben
- [x] Positionen aus CSV oder Excel in einen Beleg importieren; wiederkehrende Rechnung an frei wählbaren Stichtagen (früher fest 15./Monatsende) mit Mengen aus dem Kalender (Feiertage NRW)
- [x] Beleg: Positionsänderungen erst nach „Speichern“ gültig, sonst verwerfbar; Kreditlimit-Warnung in der Kopfkarte unter Netto, auf der Kundenkarte direkt neben dem Kreditlimit
- [x] Gutschrift: mindert die Rechnung nicht mehr von selbst, eigener Minusposten in den offenen Posten, Verrechnung von Hand mit Betrag auf der Zahlungsseite, Aufheben per Gegenbuchung; Filter „ohne Storno/Gutschrift“ blendet nur noch Erledigtes aus
- [x] Shop: Artikeldaten (Texte, Maße, Hersteller, Kategorie, Bilder, Bestand, Preise, neue Artikel) per Knopf oder im Takt, offline gesetzte Artikel werden im Shop deaktiviert
- [x] Position nachträglich in eine Gruppe verschieben (Position → „bearbeiten“ → Gruppe), auch in eine andere oder heraus; abgelehnt, wenn sich dadurch Belegwert oder Steuer ändern würden (Steuersatz, Rabatt der Gruppe, nicht centgenauer Rabatt, anderer Lieferschein, negativer Gruppenpreis)
- [x] Layout: eine Seitenleiste links mit allen Bereichen als aufklappbare Gruppen (Untermenüs, aktive Gruppe offen), einklappbar auf Symbole, unter 900 px als Schublade; Mandantenwahl bleibt in der Kopfzeile
- [x] Pfad im Kopfbereich neben dem Mandanten: Bereich › Menüpunkt (verlinkt) › aktuelle Seite aus dem Seitentitel, z. B. „Einkauf › Bestellungen › BE2026-00003“; Zuordnung der Seiten in `apps/kern/kontext.py` (`PFAD`), bei neuen Menüpunkten dort mitpflegen
- [x] Stammtabellen mit Nummer, in der Verwaltung pflegbar und erweiterbar: Anrede (1 Firma, 2 Herr, 3 Frau; für Kunde, Lieferant und Ansprechpartner, vorhandene Texte übernommen), Zahlungsbedingungen „zabe“ (Liste aus Mention übernommen, real/intern/stop = fällig/Mahnung/Lieferstopp), Versandarten „vers“ (Nummer, Versand, Verpackung, Versicherung). In Kunden- und Lieferantenmaske als Suchfeld „8 – Vorkasse“ (Nummer oder Text tippen); Standard-Versandart des Kunden wird samt Kosten in neue Belege übernommen (beim Lieferanten nur hinterlegt, Einkaufsbestellungen haben noch kein Versandfeld)
- [x] Artikelmaske: Nachfolger als Suchfeld, ohne Suchbegriff oben Artikel mit ähnlicher Bezeichnung; „*“ + Enter (oder Suchbegriff mit *) öffnet eine Tabelle Artikelnr. | Bezeichnung | Bestand (Mandant) | VK3 netto, Auswahl per Klick oder ↑↓ + Enter
- [x] Artikelsuche (Belegmaske, Tabellensuche, Nachfolger) mit Suchsyntax: `*` = alle, `>5` = Lagerbestand des Mandanten über 5, `%` wie SQL-LIKE (`Netz%` beginnt mit, `%750W%` enthält, `%Gold` endet mit), sonst Wörter in beliebiger Reihenfolge; in der Belegmaske öffnet `*` (oder `>`/`%`) + Enter eine Tabelle Artikelnr. | Bezeichnung | Bestand | verfügbar | Preis netto (Preis des Kunden). Code: `apps/stammdaten/suche.py`
- [x] Lieferantenrechnungen: Filter „SEPA erstellt, nicht gebucht“ (SEPA-Datei erzeugt, Zahlung noch nicht gebucht); „Nicht bezahlt“ zeigt wieder auch diese (zeigte vorher nur noch nicht exportierte)
- [x] Bankabgleich: Teilzahlungen mit Rechnungsnummer werden beim Import gebucht (vorher nur Vorschlag, der Kunde blieb voll offen); mehrere Nummern im Verwendungszweck oder in der Eingabe verteilen den Betrag der Reihe nach; Knopf „Vorschläge nachbuchen“ für ältere Importe; Storno gibt einen aufgeteilten Umsatz erst frei, wenn alle seine Zahlungen storniert sind
- [x] Kostenstelle am Artikel (Stammdaten, auch Leistungsartikel und Kopie); im DATEV-Export noch nicht enthalten (Aufteilung je Kostenstelle mit dem Steuerberater abstimmen)
- [x] Layoutfarbe je Mandant (Verwaltung → Mandanten, Farbwähler): Akzentfarbe und Streifen über der Kopfzeile, auch in der Verwaltung; vorbelegt Blau für OKAY, Grün für ZONE. Pfad im Kopfbereich beginnt am Ende der Seitenleiste
- [x] Seitenleiste: ohne gewählten Kunden kein Hinweistext, Menüpunkt „Shop & Online“; wiederkehrende Rechnungen: Bearbeiten, Pausieren/Aktivieren und Löschen als Symbole
- [x] Dashboard als Startseite (`/`): offene Kundenbestellungen, offene Posten, Umsatz netto (Rechnungen minus Gutschriften) mit Vorzeitraum, Lieferanten, Zeitraumvergleich, Umsatzverlauf, Top Kunden/Artikel/Lieferanten, Kanäle, Arbeitsliste; Zeitraum 7/30/90/365 Tage
- [x] Einkauf: Bestellungen, Wareneingang, Lieferantenrechnungen, RMA, Bestellvorschlag, SEPA-Export, Preislisten-Import (Typ P als eigene Tabelle)
- [x] Lager: Bestand je Mandant, Bewegungen, Inventur, Seriennummern, Bestandsregel (nie unter 0)
- [x] Rückverfolgung für Reklamationen: Seriennummer merkt sich den Wareneingang und damit den Lieferanten (Seriennummern-Suche, Artikelseite), Karte „Wareneingänge“ am Artikel, Knopf „RMA“ belegt Lieferant, Artikel und Seriennummer vor
- [x] Finanzen: Offene Posten, Zahlungen mit Storno, Mahnwesen, Bankimport, Kassenbuch, DATEV
- [x] Werkstatt, ESD-Keys, Shopware-Anbindung
- [x] „In Lieferantenbestellung kopieren“: Lieferantenwahl mit Preisvergleich bei mehreren Positionen (günstigste Aufteilung, „Alles bei …“, Auswahl je Position)
- [ ] Anmeldung mit 2FA (bewusst verschoben)
- [x] Lastschrift-Einzug (pain.008)
- [x] Ausgehende E-Rechnung (ZUGFeRD im PDF, XRechnung als XML am Beleg); die Käuferanschrift kommt vom Beleg. Offen: PDF/A-3 extern prüfen, PDF/XML archivieren
- [x] Proforma-Rechnung, Nachlieferschein, wiederkehrende Rechnungen
- [x] Versand mit DHL-Paket-Label und Sendungsnummer
- [x] Rechte: Admin (is_staff/Superuser) und Mitarbeiter; Storno, Lastschrift aufheben und Preislisten-Einrichtung nur Admin
- [x] Kein Passwort-Reset per Mail: unter `/konto/` nur Anmelden, Abmelden und Passwort ändern; vergessene Passwörter setzt ein Admin in der Verwaltung (Benutzer → Passwort) neu
- [x] Artikelmaske mit Registerkarten (Lager → „+ Neuer Artikel“), neue Artikelfelder, Artikelansicht mit bestellt/letztem Wareneingang/Lieferanten
- [x] EK-Preis netto beim Anlegen/Bearbeiten (Typ A, Komma-Eingabe) als Basis ohne Preisimport; Herkunft (manuell/Import) mit Datum; der Import-EK ist maßgebend, der Wareneingang ändert den EK nicht (Lagerwert mit demselben EK); Artikelliste mit Spalte EK netto bzw. „EK fehlt“; in der Artikelmaske und in Verwaltung → Artikel rechnen VK und Aufschlag je Stufe beim Tippen gegenseitig aus dem EK nach, je Stufe „fix“ für VK (fester Preis) oder Aufschlag (VK folgt dem EK; ohne EK und Aufschlag abgelehnt), Häkchen „Aufschlag VK1 % für alle übernehmen“; Reiter Preise als Raster (EK und Häkchen, VK1–VK3, Aufschlag VK1–VK3)
- [x] Alle Belege (kundenübergreifende Liste), Versandarten-Stammtabelle mit Standardkosten, Wiedervorlage mit Startseiten-Übersicht, Belegkopf (Bearbeiter, Bezugnahme, Liefertermin, Angebot gültig bis)
- [x] Laufende Nummern in der Verwaltung einstellbar, Belegnummern durchlaufend über den Jahreswechsel
- [x] USt-ID online prüfen bei Kunden und Lieferanten (BZSt und VIES) mit dauerhaftem Nachweis
- [x] Alle Mailtexte an Kunden und Lieferanten unter Verwaltung → Mailvorlagen je Mandant (Tabelle `verkauf_mailvorlage`): Belege, Bestellung an Lieferanten, Lizenzschlüssel, Versandmitteilung, Mahnung, drei Werkstatt-Statusmails. Fehlende Zeilen entstehen beim Öffnen der Liste mit dem Standardtext aus `apps/verkauf/mail.py`, vorhandene bleiben unverändert; Anlegen und Löschen gesperrt, unbekannte Platzhalter werden beim Speichern abgelehnt. Der Mahntext bleibt an der Mahnstufe, weil er auch im PDF steht, und kommt über `{mahntext}` in die Mail
- [x] Mahnwesen: Spalte „Versand“ je Mahnung (gesendet am/an, Versand fehlgeschlagen, gedruckt am, noch nicht gesendet), Filter „Nur noch nicht versendete“. Mehrere Mahnungen mit Haken auswählen: „Ausgewählte drucken“ (ein PDF, jede Mahnung auf neuer Seite, setzt den Druckvermerk) und „Ausgewählte senden“ (erst Übersicht mit Empfängern, dann Versand mit Standardtext; ohne E-Mail-Adresse wird übersprungen und genannt). Je Sammelversand höchstens 15 Mails (`SAMMELVERSAND_MAX` in `apps/verkauf/mail.py`, Vorgabe des Mailanbieters; gilt für jeden künftigen Sammelversand von Belegen). Der Nachweis je Mail bleibt im Mailprotokoll
- [x] Standard-VK je Mandant (Verwaltung → Mandanten → Artikel und Preise, Vorgabe VK3): Artikel & Bestand, die Tabellensuche der Artikelfelder und die Artikellisten der Verwaltung zeigen diese Preisstufe. Der Preis in Belegen richtet sich unverändert nach der Preisstufe des Kunden
- [x] Artikelstamm erweitert (Entscheidung 19.11.2026, Abschnitt 18): Kennungen (ASIN, SKU, FNSKU, ISBN, UPC, eBay-ID) in allen Suchen, UVP/EK3/Sonder-EK/Preisgrenzen/Preiseinheit/Grundpreis als Felder, weitere Kategorien, Steuersatz 19 % als Vorgabe, Historie auf der Artikelseite (Migration `stammdaten/0033`). Preisgrenzen-Warnung, Preiseinheit-Rechnung und Grundpreis im Beleg umgesetzt (Migration `verkauf/0041`). Shop-Anbindung der weiteren Kategorien, des Grundpreises und der UVP umgesetzt (Migration `shop/0008`)
- [x] Artikel & Bestand: Spalte „Onl.“ als Symbol (im Shop sichtbar oder nicht, Grund im Tooltip; online gesetzte Artikel, die noch nicht mit dem Shop des Mandanten abgeglichen sind, gelb „noch nicht im Shop sichtbar“)
- [x] SMTP-Passwort je Mandant in der Verwaltung (verschlüsselt, Abschnitt 8.5), Knopf „Testmail senden“
- [x] Verwaltung: Kopf und Brotkrümel bleiben beim Scrollen stehen, rechts in den Brotkrümeln „Speichern“ (wie Djangos „Sichern“, nur auf Seiten mit Formular), Vorlage `templates/admin/base_site.html`
- [x] Proforma-Rechnung als Vorkasse (Issue 57): Vorkasse, Zahlstatus, Umwandlung in die Rechnung mit Verrechnung, Guthaben, Storno-Beleg, Anzahlungen auf Bestellungen, Offene Proformas, Bankabgleich über PF-Nummern
- [x] Artikel: Kategorie per Suchfeld mit Sofortfilter (Issue 58) in Artikelmaske und Verwaltung – Suche über Pfad und Nummer, mehrere Wörter, „Zuletzt verwendet“ oben, Tastatur; gemeinsamer Baustein `templates/_suchauswahl.html` (`<select data-suchauswahl>`)
- [x] Teilübernahme: Die übernommene Menge wandert aus der Bestellung in Lieferschein/Rechnung/Proforma (Bestellung behält den Rest), Verlauf `BelegUebernahme`, Mitziehen/Rückbuchen, PDF-Block, Befehl `rueckstand_umstellen` für Altdaten
- [x] Überzahlung: Guthaben des Kunden beim Zahlung buchen anderer Rechnungen vorschlagen und nachvollziehbar verrechnen
- [x] Vorläufige Belege: Neue Belege, Übernahmen, Umwandlungen und Kopien aus der Belegmaske sind bis „Speichern: abschließen & Nummer vergeben“ vorläufig (ohne Nummer) und werden sonst komplett zurückgenommen; `BelegVorgang`, Befehl `belege_verwerfen`, Abo legt eine Bestellung an; aus einem nicht gespeicherten Angebot ohne Rückfrage, das Angebot wird erst mit dem neuen Beleg nummeriert, beim Verwerfen ist es wie vorher (Abschnitt 18)
- [x] Shop: fehlende Kategorie samt Elternkategorien beim Senden eines Online-Artikels anlegen, Zuordnung über den Pfad (Issue 59)
- [x] Deutsche Post (INTERNETMARKE) im Versand; Shop-Kategorien werden beim Senden eines Online-Artikels zugeordnet oder angelegt (Issue 59)
- [ ] eBay, Kaufland, Newsletter mit Double-Opt-in
- [x] Dashboard mit Umsatz, Top-Listen und Arbeitsliste; Wiedervorlagen auf der Startseite
- [x] Aussehen je Mandant frei wählbar (16.11.2026, Verwaltung → Mandanten → Aussehen): Flächenfarben für Kopfbereich, Seitenleiste und Karten getrennt – neutral, „gleiche Tonart“ (aus der Layoutfarbe abgeleitet) oder eigene Farbe je Bereich; Schrift, Ränder und Akzent passen sich automatisch an (Kontrast), dunkler Modus wird abgeleitet (`apps/kern/layout.py`). „Aussehen zurücksetzen“ (Knopf und Sammelaktion): Layoutfarbe OKAY `#2b72b4`, ZONE `#83bb2a`, Flächen neutral
- [x] Neue Oberfläche (15.11.2026): ruhigere Optik für alle Seiten (`templates/_optik.html`, hell und dunkel, Layoutfarbe des Mandanten gilt weiter), Dashboard mit Kennzahlen-Kacheln (Umsatz Zeitraum/heute/Monat, Lagerwert, offene Posten) und Diagrammen (Verlauf, 12 Monate, Kanäle, offene Posten nach Alter)
- [x] Flexible Listen: Jeder Benutzer richtet jede Liste über das Zahnrad für sich ein (Spalten ein-/ausblenden, Reihenfolge, Breite, Zeilendichte); gespeichert je Benutzer und Liste (`kern.Listenansicht`). Ein Admin setzt sie in der Verwaltung zurück: Benutzer markieren → Aktion „Listenansichten zurücksetzen“
- [x] Schnellbedienung: **Strg+K** (oder Knopf „Wohin?“ in der Kopfzeile) öffnet ein Suchfenster für Kunden, Artikel, Belege, Lieferanten, Bestellungen, Reparaturen und alle Menüpunkte; Belege und Bestellungen nur des aktiven Mandanten
- [x] Hilfe in der Schnellbedienung (19.11.2026): Fragen wie „Wie buche ich eine Kundenrechnung?“, „Wie erstelle ich eine Mahnung?“ oder Stichworte („skonto“, „gutschrift ohne rechnung“) zeigen die Gruppe **Hilfe** (höchstens 5 Treffer; bei einer Frage – beginnt mit wie/wo/was/wann/kann/warum … oder endet mit „?“ – ganz oben, sonst hinter den übrigen Treffern). Enter oder Klick öffnet die Antwort in der Palette: die Möglichkeiten als Liste mit kurzen Wegen („Verkauf → Kunde → … → Speichern: abschließen & Nummer vergeben“) samt Hinweisen aus den Fachregeln und dem Knopf „Hingehen“; Esc oder „Zurück“ führt zur Trefferliste. Die rund 60 Einträge stehen als feste Texte in `apps/kern/hilfe.py` (Frage, Stichworte, Wege, Sprungziel `url_name`); Bewertung über Wörter in Frage und Stichworten (Groß/Klein, Umlaute als ae/oe/ue, Stoppwörter ignoriert). Ändert sich ein Knopf oder eine Fachregel in Abschnitt 18, den Weg dort nachziehen; `apps/kern/test_hilfe.py` prüft, dass jedes Sprungziel noch auflösbar ist
- [x] Lieferanten-Gutschrift (19.11.2026): „Gutschrift erhalten“ an der WE-Rücksendung erfasst Nr., Datum, Netto/USt/Brutto und Notiz (`Lieferantengutschrift`); Verrechnung (auch teilweise) mit offenen Lieferantenrechnungen senkt deren Zahlbetrag und SEPA-Betrag, voll verrechnet = bezahlt; Rückgängig und Zahlungsstorno mit Pflichtgrund; Zahlung des Lieferanten per Knopf oder aus dem Bankabgleich; Restguthaben in Offene Posten Lieferanten (Abschnitt 18)
- [x] Lieferanten-Gutschrift ohne Rücksendung erfassen (19.11.2026): Einkauf → Lieferanten-Gutschriften (Erfassen und Liste „Mit Restguthaben“/„Alle“), Hilfe-Eintrag
- [x] WE-Rücksendung über den Lieferanten (19.11.2026): „+ WE-Rücksendung“ mit Auswahl des gebuchten Wareneingangs auf der Lieferantenseite, Menüpunkt in der Seitenleiste entfernt
- [x] Lieferanten-Gutschriften im Lieferantenkonto (19.11.2026): Belegart „Lieferanten-Gutschrift“ in der Belegliste des Lieferanten und in den offenen Belegen (offen = Restguthaben > 0, Betrag = Restguthaben), „Offenes Guthaben“ im Kopf
- [x] Lieferantenkontoauszug (19.11.2026): Symbol „Kontoauszug“ beim Lieferanten; Rechnungen +, Zahlungsausgänge und Skonto −, Gutschriften −, Auszahlungen des Lieferanten auf Gutschriften +, Verrechnungen in Klammern (ohne Saldowirkung), laufender Saldo (negativ = Guthaben)
- [x] Kontoauszug als PDF (19.11.2026): „PDF drucken“ beim Kunden- und Lieferantenkontoauszug (A4 mit Anschrift, Stand, Buchungen, Schlusssaldo, Seitenzahl)
- [x] Lieferantenrechnung mit mehreren Wareneingängen (20.11.2026): Auswahl als Tabelle mit Bestell-Nr. und Lieferschein, Summe der angehakten Wareneingänge und Differenz zur Rechnung sofort sichtbar
- [x] Wareneingänge schon beim Erfassen der Lieferantenrechnung verknüpfen (20.11.2026): nach Wahl des Lieferanten erscheinen dessen freie gebuchte Wareneingänge zum Anhaken, Netto wird aus der Summe vorgeschlagen, Differenz sofort sichtbar
- [x] Ansprechpartner im Kopf von Kunde und Lieferant auswählbar (20.11.2026): Anrede, Name, Abteilung, Telefon/Durchwahl, Mobil, E-Mail; ohne Auswahl Hauptkontakt, sonst der erste; Auswahl im Browser gemerkt; beim Lieferanten nur Ansprechpartner des Mandanten. Neuer Beleg vom Kunden aus („+ Angebot“, „+ Bestellung“, „+ Gutschrift“) übernimmt den gewählten Ansprechpartner (Vorgabe: Hauptkontakt, sonst der erste)
- [x] Ansprechpartner in der Lieferantenbestellung (20.11.2026): „+ Bestellung“ übernimmt den im Lieferantenkopf gewählten (Vorgabe: Hauptkontakt, sonst der erste), änderbar im Tab „Versand & Konditionen“, „z. Hd.“ auf dem PDF, Empfänger der Bestell-Mail (Migration einkauf 0026, nur neues Feld)
- [x] Wareneingang ohne Position wird nicht aufbewahrt (20.11.2026): ungebucht, leer, keiner Rechnung zugeordnet, kein Ersatz → beim Öffnen von Lieferant, Lieferantenübersicht oder Wareneingangsliste gelöscht (eigene sofort, die anderer Benutzer nach 2 Minuten ohne Änderung), Nummer wird wieder frei, wenn sie die letzte war
- [x] Ansprechpartner im Kundenbeleg änderbar (20.11.2026): im Belegkopf auswählbar (Hauptkontakt zuerst, „keiner“ möglich), speichert sofort beim Wechsel, zeigt Anrede, Name, Abteilung, Telefon, Mobil, E-Mail; nur Ansprechpartner dieses Kunden; gesperrte Belege zeigen ihn nur an (der bisherige Eintrag im Tab „Versand & Zahlung“ entfällt)
- [x] Bestellvorschlag und Tab „Bedarf“ zusammengeführt (20.11.2026): eine gemeinsame Tabelle (`einkauf/_bedarf_tabelle.html`, `services.bedarf_zeilen`); im Bestellvorschlag Lieferant je Artikel wählbar (vorgewählt der günstigste, nur Lieferanten mit Preis, keine gesperrten) und Übersicht „ergibt n Bestellungen“ mit Summe je Lieferant; in beiden steht unter „vorhanden“, in welcher Bestellung (auch Entwurf) die Ware schon ist
- [x] Eine Blätterleiste für alle Tabellen (20.11.2026): `kern.seiten.blaettern` + `templates/seiten.html` für die eigenen Listen, `BlaetternAdminMixin` + `templates/admin/pagination.html` für die Verwaltung; „Seite x von y (a–b von n)“, « ‹ zurück · weiter › », dazu „pro Seite“ 25/50/100/200/500 (je Liste in der Sitzung gemerkt). Neu bei Inventur (ersetzt die Grenze von 200 Treffern) und in der Verwaltung bei Kunden, Lieferanten, Artikeln (auch G) und Leistungsartikeln (L); deren Suchfeld filtert jetzt schon beim Tippen
- [x] WE-Rücksendung (19.11.2026): Auswahl beim Lieferanten zeigt nur Wareneingänge mit noch zurücksendbarer Menge; ist alles zurückgesendet, klarer Hinweis statt leerem Mengenfeld
- [ ] Auswertungen mit Marge, Excel-Ausgabe und freiem Zeitraum
- [x] Gesamtprüfung vom 14.11.2026 (`docs/pruefbericht-2026-11-14.md`): Abschnitt A und C behoben, darunter automatisches Kunden-Skonto, Lastschrift erst mit der Gutschrift im Bankabgleich, Kasse nie im Minus, Belegnummern bleiben nach dem Löschen verbraucht, Kundensperre auch bei Kopie und wiederkehrender Rechnung, Warnung bei doppelter EAN und doppeltem Lieferanten
- [x] Python 3.12 (eigene Umgebung auf dem Server) und Django 5.2; hochgeladene Preislisten liegen außerhalb des öffentlichen Medienordners (`PRIVAT_ROOT`)

**Einrichtung**

- [x] Code lokal unter `D:\Desktop\github\wwok`
- [x] Repo `akuvvet/wwok` angelegt, `main` = Stand auf dem Server
- [x] Subdomain `erp.okaycomputer.de` in Plesk, Datenbank `wwok` (MariaDB), HTTPS
- [x] Server: Pakete, Deploy-Key, `git clone`, venv, `.env`, `migrate`, `collectstatic`, Superuser
- [x] Gunicorn-Dienst `wwok` (Port 8010) und nginx-Weiterleitung, Rechte gesetzt
- [x] Zeitpläne in `/etc/cron.d/wwok` (Shop-Abgleich, Preislisten)
- [ ] Zeitpläne `belege_verwerfen` und `wiederkehrende_rechnungen` in `/etc/cron.d/wwok` ergänzen (Abschnitt 13.10)
- [x] Mailversand je Mandant getestet
- [ ] Tägliches Backup der Datenbank prüfen (Plesk-Backup und externe Kopie)
- [ ] Claude-GitHub-App installieren (nur nötig für Push aus der Web-Sitzung von Claude)

## 16. Nächste Schritte

0a. Auf dem Server nach dem Deploy: Cron-Zeilen für `belege_verwerfen` (jede Minute) und `wiederkehrende_rechnungen` (täglich) in `/etc/cron.d/wwok` eintragen (Abschnitt 13.10); `python manage.py belege_verwerfen` im Probelauf prüfen. Bestehende Altentwürfe sichten: Sie bleiben unverändert (nicht vorläufig) und müssen von Hand abgeschlossen oder gelöscht werden
0. Auf dem Server nach dem Deploy: `python manage.py rueckstand_umstellen` im **Probelauf** prüfen, dann mit `--ausfuehren` die Bestellungen mit alten Teilübernahmen (auch beim Kunden versandte) auf den Rest umstellen
0b. Artikelstamm (Entscheidung 19.11.2026): Shop-Anbindung erledigt, nichts offen
1. Lastschrift (pain.008) ist umgesetzt; noch prüfen, ob die Gläubiger-ID für beide Mandanten eingetragen ist
2. **Anmeldung mit 2FA**, Limit für Anmeldeversuche und Benutzerrechte je Mandant (Prüfbericht B4, E5)
3. Fehlerhafte Rechnung RE2026-00003 korrigieren (Storno oder Bestandsnachbuchung)
4. Ausgehende E-Rechnung ist gebaut; vor 2027 noch extern als PDF/A-3 prüfen und PDF/XML archivieren
5. **Auswertungen** mit Marge, Excel und freiem Zeitraum (Dashboard und Wiedervorlagen sind umgesetzt)
6. Offene Punkte aus `docs/pruefbericht-2026-11-14.md`: Abschnitt B (Betrieb/Sicherheit), D (offene Anforderungen), E (Entscheidungen), dazu die Fragen an den Steuerberater (Mahngebühr als Forderung, DATEV-Konten PayPal/Karte, Skontokonto, Rechnungsdatum)
7. eBay, Kaufland, Newsletter; Versand mit GLS/DPD/UPS (DHL und Deutsche Post sind umgesetzt)
8. Backup und Wiederherstellung einmal real durchspielen
9. Bestehende Mailvorlagen auf dem Server auf alten Wortlaut prüfen (z. B. „Auftrag“ statt „Bestellung“)
10. Umwandeln: Ein per Mail verschicktes Angebot lässt sich nach der Umwandlung nicht originalgetreu nachdrucken (kein PDF-Archiv). Klären, ob Art und Nummer am Versandnachweis gespeichert werden sollen
11. Hilfe der Schnellbedienung (`apps/kern/hilfe.py`): Wege einmal von Hand in der Oberfläche gegenlesen (Knopfnamen, Reihenfolge) und im Browser prüfen (Tastatur, Screenreader); bei jeder Änderung an Menü, Belegmaske oder Fachregeln den betroffenen Eintrag anpassen

## 17. Regeln für das Repository

- Die `.env` mit Passwörtern kommt **nie** ins Repository.
- `main` = Stand, der auf dem Server läuft. Entwicklung in eigenen Branches.
- Jede Änderung an Modellen braucht eine Migration (`python manage.py makemigrations`), die mit eingecheckt wird.
- Echte Kunden- oder Belegdaten **nie** einchecken.
- **Versionsnummer** steht an genau einer Stelle (`config/__init__.py`) und erscheint in der Fußzeile jeder Seite. Zählung: letzte Stelle bei Korrekturen (0.9.1), mittlere bei neuen Funktionen (0.10.0), 1.0 erst nach Abschnitt B des Prüfberichts und den Antworten des Steuerberaters. Jede Version bekommt eine Markierung im Repository (Git-Tag `v0.9.0`).
- Änderungen am Pflichtenheft im Claude-Dokument pflegen und `docs/pflichtenheft.md` neu exportieren.

## 18. Fachregeln im Programm (verbindlich)

Diese Regeln sind mit dem Nutzer abgestimmt und im Code umgesetzt. Änderungen nur nach Rücksprache.

| Thema | Regel |
| --- | --- |
| Artikel Typ A | eigene Tabelle `Artikel` mit Lagerbestand |
| Artikel Typ P | **eigene Tabelle `Preislistenartikel`**, getrennt von A; neben dem Artikel steht der Kurzname des Lieferanten (z. B. API, Kosatec, Ingram). Ein eigener Artikel A entsteht daraus **nie still**, sondern nur nach Rückfrage im Wareneingang (Hinweiskasten „anlegen und übernehmen“, „doch geliefert“, Scan mit Bestätigung oder „Im Formular anlegen …“: die Artikelmaske öffnet sich mit den Lieferantendaten, angelegt und in den Wareneingang übernommen wird erst beim Speichern) und mit einer Nummer aus dem eigenen Nummernkreis; ein vorhandener Artikel mit gleicher Herstellernummer wird nur verknüpft, wenn auch Hersteller oder EAN passen. Code-Eingabe in der Lieferantenbestellung ergibt eine P-Position, keinen Artikel (Entscheidung Nutzer 07.11.2026) |
| Typ P beim Kopieren | P gibt es nur im Angebot. In Bestellung, Lieferschein, Rechnung und Proforma werden P-Positionen nicht kopiert (eine Gruppe mit P-Unterposition zählt komplett als P): aus dem Angebot bleiben sie im Angebot stehen; aus der Bestellung nach Lieferschein/Rechnung/Proforma wandern sie in ein neues Angebot, die Bestellung ist damit erledigt, wenn sie leer ist und nichts mehr offen bleibt (bei Proforma bis zum Storno, der sie wieder öffnet). Enthält der Beleg nur P, wird nichts angelegt (Meldung). Entscheidung Nutzer 07.11.2026. **Speichern einer Bestellung mit P (Entscheidung Nutzer 18.11.2026):** „Speichern: abschließen & Nummer vergeben“ wird nicht mehr abgelehnt – die P-Positionen wandern mit Warnung in ein neues Angebot desselben Kunden (mit Nummer, nicht abgeschlossen), die Bestellung wird ohne sie gespeichert; so bleibt nachvollziehbar, was angeboten wurde. Enthält die Bestellung nur P, wird sie zum Angebot: eine neue Bestellung ohne Nummer verschwindet (keine Lücke im Nummernkreis), eine Bestellung mit Nummer bleibt leer als erledigt stehen. Lieferschein und Rechnung mit P lassen sich weiter nicht speichern |
| Preisliste neu einlesen | Artikel, die in der Preisliste nicht mehr vorkommen, werden entfernt (mit Schutz: bricht ab, wenn mehr als 50 % verschwinden würden) |
| Lagerbestand | darf **nie unter 0** fallen – in keinem Zustand: Lagerbestand und Reparaturbestand lehnen jeden Abgang ab, der unter 0 führt; „verfügbar“ (Bestand minus Reservierung) wird bei Überbestellung als 0 gezeigt, nie negativ |
| Reservierte Ware (Entscheidung 15.11.2026) | Was für eine Kundenbestellung reserviert ist, gilt für alle anderen als nicht vorhanden („wie verfügbar 0“): Lieferschein und Rechnung ohne Lieferschein lassen sich nur abschließen, wenn Bestand minus fremde Reservierung reicht. Die Reservierung des eigenen Vorgangs (Rest der Bestellung, Proforma) zählt nicht mit. Die Meldung nennt Bestand und reservierte Menge |
| Bestellung mit Anzahlung → Rechnung (Entscheidung 16.11.2026) | Geht die ganze Bestellung in die Rechnung, wird sie auch mit Anzahlung **umgewandelt** (derselbe Beleg, „vormals AB…“): Die Zahlungen hängen danach an der Rechnung und sind dort Teilzahlungen; es bleibt keine leere Bestellung zurück. Das PDF zeigt „abzüglich erhaltene Zahlung“ und „Restbetrag“ mit dem Stand bei der Umwandlung; spätere Zahlungen ändern den ausgestellten Beleg nicht. Verwerfen der nicht gespeicherten Rechnung stellt die Bestellung samt Anzahlung wieder her. Bei Teilübernahme oder schon verrechneter Anzahlung bleibt es beim neuen Beleg mit Verrechnung |
| Spalte R der Bestellung (Entscheidung 16.11.2026) | Jede Position mit Lagerartikel hat in der Bestellung einen Haken „R“ (reservieren), Standard gesetzt. Ohne Haken hält die Position keinen Lagerbestand fest – sie wartet auf den Wareneingang – und der Bestand ist für andere Bestellungen frei. Der Haken wird nur von Hand gesetzt und entfernt (auch nach dem Wareneingang); eine Proforma übernimmt ihn. Der Bestand fällt dabei nie unter 0 |
| Artikelstamm (Entscheidung 19.11.2026) | **Kennungen** (EAN, Amazon ASIN/SKU/FNSKU, ISBN mit Prüfziffer, UPC, eBay-ID) werden in jeder Artikelsuche (Artikelliste, Inventur, Schnellsuche, Belegmaske, Tabellensuche, Wareneingang-Scan) wie die EAN gefunden; die ISBN wird ohne Bindestriche gespeichert. **Preisgrenzen** `vk_min`/`vk_max` (netto, min ≤ max); die Belegmaske warnt an der Position rot („unter Mindest-VK x,xx €“ / „über Höchst-VK x,xx €“), wenn der Netto-Preis je Preiseinheit nach Positionsrabatt die Grenze verletzt; nur Warnung, keine Sperre, nicht im PDF, nur solange die Positionen änderbar sind, nicht in der Gutschrift. **Preiseinheit** „VK gilt je n Stück“ (Standard 1, mindestens 1): die Position übernimmt sie beim Anlegen aus dem Artikel (`Position.preiseinheit`, auch `AboPosition`; Kopie, Übernahme und Umwandlung behalten sie, Gruppenköpfe und Shop-Bestellimport haben immer 1). Positionsbetrag = Menge × Einzelpreis ÷ Preiseinheit, dann Rabatt, Rundung auf Cent wie bisher (in `Position.netto_gesamt`); PDF und Belegmaske zeigen „je 100 Stück“ beim Preis, die E-Rechnung (ZUGFeRD/XRechnung) führt den Preis je n Stück mit `BasisQuantity` (BT-149). **Grundpreis** nach PAngV: Preis × Faktor je Bezugseinheit (z. B. kg, l), kaufmännisch auf 2 Stellen; die Artikelseite zeigt ihn zum VK1; im Beleg-PDF (Angebot, Bestellung, Rechnung, Proforma; nicht Lieferschein/Gutschrift) und in der Belegmaske steht unter der Bezeichnung „Grundpreis: 12,50 € / kg“, gerechnet aus dem Brutto-Preis je Stück (Einzelpreis ÷ Preiseinheit × (1 + Steuer), nach Positionsrabatt). Dazu UVP, EK3 und Sonder-EK mit Zeitraum. **Weitere Kategorien** zusätzlich zur Hauptkategorie: der Kategoriefilter (Artikelliste, Inventur) findet Artikel auch darüber, sie gelten auch im Shop (Anbindung siehe „Artikeldaten an den Shop“). Beim **Neuanlegen** ist der Steuersatz mit 19 % vorbelegt. **Historie** auf der Artikelseite aus dem Änderungsprotokoll (alt → neu, höchstens 200 Einträge, Mandantendaten nur des aktiven Mandanten) samt Eingabedatum und letzter Änderung  **Preiseinheit und Preise:** Fester VK, UVP und Kundensonderpreise werden je Preiseinheit eingetragen; ein VK aus EK + Aufschlag wird mit der Preiseinheit multipliziert (der EK gilt je Stück), ebenso der Vorschlag im Preis-Radar. Der Shop bekommt Preis und UVP je Stück. |
| Minuspreise (Entscheidung 15.11.2026) | Negative Einzelpreise sind erlaubt (Nachlass, Pfand), die Belegsumme darf aber nicht negativ sein: ein solcher Beleg lässt sich nicht abschließen. Erstattungen laufen über die Gutschrift zur Rechnung |
| Deploy und Migrationen (Entscheidung 15.11.2026) | Der Deploy bleibt automatisch. Eine neue Migration, die Spalten oder Tabellen löscht (RemoveField, DeleteModel, RunSQL mit DROP/DELETE/TRUNCATE), hält ihn vor dem Merge an (`deploy/loeschende_migrationen.py`). Sie geht nur mit der Zeile „# Datenverlust freigegeben: Grund“ in der Migration durch, und die kommt erst nach ausdrücklichem Auftrag hinein |
| Lieferschein und Rechnung | nur mit ausreichendem Bestand abschließbar, **Druck/PDF ebenfalls gesperrt**, solange der Bestand nicht reicht |
| Rechnung aus Lieferschein | erst nach **abgeschlossenem** Lieferschein (der bucht das Lager ab) |
| Übernehmen (Umwandlung) | Angebot → Bestellung und Bestellung → Rechnung (direkt, Knopf „Als Rechnung übernehmen“) wandeln den Beleg **um**: derselbe Datensatz bekommt die neue Belegart und eine **neue Nummer** aus dem Kreis der neuen Art (die Nummer vergibt erst „Speichern: abschließen & Nummer vergeben“ – der Beleg ist bis dahin vorläufig, siehe „Vorläufige Belege“ –, damit die Nummernkreise lückenlos bleiben; Shop- und Abo-Bestellungen werden weiter sofort nummeriert); Belegdatum = heute, Entwurf, „gültig bis“ und Liefertermin werden geleert. Die alten Nummern stehen im Verlauf (Art, Nummer, Datum, Benutzer), bleiben vergeben und werden bei der Suche gefunden; der PDF-Bezug nennt die letzte frühere Nummer. Bestellung → Lieferschein und Lieferschein → Rechnung ergeben weiter einen neuen Beleg. Wird der vorläufige Folgebeleg verworfen, steht die Bestellung wieder wie vorher. Entscheidung Nutzer |
| Teillieferung | bei mehreren Positionen und teilweisem Bestand: Rückfrage; lieferbare Positionen werden als eigener Beleg abgeschlossen, der Rest bleibt unverändert als Entwurf. Versandkosten laufen mit der Teillieferung. Bei Teilübernahme (Typ P, Engpass/Teilmenge, schon vorhandene Folgebelege auch storniert oder frühere Kopien, Anzahlungsverrechnungen auf der Bestellung) wird nicht umgewandelt: Es entsteht ein neuer Beleg, die übernommene Menge wandert dorthin und der Rest bleibt im Ursprungsbeleg (siehe „Teilübernahme aus der Bestellung“) |
| Teilübernahme aus der Bestellung | „Kunde hat 10 bestellt, Teillieferung 7, dann muss nicht 10 in der Bestellung sein, sondern nur 3.“ Bei jeder Teilübernahme aus einer Bestellung in Lieferschein, Rechnung oder Proforma (Übernehmen oder Kopieren als) **wandert** die übernommene Menge aus der Bestellung in den Folgebeleg; die Bestellung behält nur den Rest – überall: Listen, Beleg, PDF, Reservierung, Dashboard. Volle Positionen werden ganz umgehängt, bei Teilmengen entsteht eine neue Position im Folgebeleg (Preis, Rabatt, Steuer, Zusatztext bleiben; Seriennummern wandern mit). Unterpositionen einer Gruppe werden **anteilig** geteilt (7 von 10 PCs → 14 von 20 RAM); lässt sich das nicht gleichmäßig teilen, wird die Übernahme mit Meldung abgelehnt. Eine Bestellung ohne Position ist **erledigt** (abgeschlossen) und bleibt mit ihrem Verlauf in den Listen. Der unveränderliche **Verlauf** (`BelegUebernahme`: Menge, Artikel, Zeit, Benutzer, übernommen / zurückgebucht / umgestellt) steht an der Bestellung („7 übernommen in Rechnung …, 3 offen“, Kasten „Verlauf der Übernahmen“) und am Folgebeleg („Aus der Bestellung übernommen“); das PDF der Bestellung trägt unter den Positionen den Block „Bereits geliefert/berechnet: 7 × … mit RE…“. Versandkosten, Verpackung und Versicherung laufen nur mit dem ersten Folgebeleg. Doppelklick oder zweiter Tab zieht nichts doppelt ab (Sperre, offene Menge wird neu gelesen; sonst Meldung und nichts übernommen). Die vollständige Übernahme als **Umwandlung** (Angebot → Bestellung, Bestellung → Rechnung im selben Beleg) bleibt unverändert |
| Mitziehen und Rückbuchen | Wird im Entwurf von Lieferschein, Rechnung oder Proforma die Menge einer übernommenen Position **gesenkt**, geht die Differenz zurück in die Bestellung; wird sie **erhöht**, kommen weitere Stück aus dem Rest der Bestellung, höchstens bis Rest 0 (darüber hinaus ist es Zusatzmenge, die Bestellung bleibt unberührt). Löschen der Position, Löschen des Rechnungs-/Proforma-Entwurfs und das **Storno der Proforma** buchen die übernommene Menge zurück (fehlt die Bestellposition, wird sie neu angelegt; eine vom Programm erledigte Bestellung wird wieder geöffnet, eine beim Kunden versandte behält ihren Status). Jede Bewegung steht im Verlauf |
| Altdaten (Teilübernahme alten Musters) | Früher blieb die Bestellmenge stehen und die Kopie wurde abgezogen. Solche Positionen (`Position.verschoben` = falsch) rechnen weiter richtig. Der Management-Befehl `rueckstand_umstellen` stellt Bestellungen auf den Rest um, auch beim Kunden versandte (abgeschlossene; ihr Status bleibt) (Standard Probelauf, geändert wird nur mit `--ausfuehren`, optional `--mandant`): Bestellungen mit aktiver Proforma werden nur gemeldet; Positionen mit Rest 0 bleiben mit Menge 0 stehen (nichts wird gelöscht), ist alles 0, ist die Bestellung erledigt; je Kopie entsteht ein Verlaufseintrag „umgestellt“ |
| Beleg ohne Position | kann nicht abgeschlossen, versendet oder weitergeführt werden |
| Löschen | Vorläufige (nicht gespeicherte) Belege werden nicht gelöscht, sondern **verworfen** (alles zurück, siehe „Vorläufige Belege“). Angebot, Bestellung und noch nicht nummerierte Rechnungs-/Gutschriftentwürfe dürfen gelöscht werden, sofern keine Folgebelege, Mails, Zahlungen oder Lagerbuchungen daran hängen; Lieferscheine und abgeschlossene Rechnungen nicht; ein aus einer Bestellung umgewandelter Rechnungsentwurf ebenfalls nicht (die frühere Bestellnummer müsste erhalten bleiben) |
| Sammelrechnung | die Auswahl „Ausgewählte Lieferscheine in Rechnung umwandeln“ erscheint nur bei **mindestens zwei** offenen Lieferscheinen; das Lieferscheindatum steht bei der Position |
| ESD-Keys | nur für Artikel der Kategorie **Software** |
| ESD ohne Key | Fehlen beim Abschluss von Lieferschein oder Rechnung Keys, fragt das Programm nach; nach Bestätigung wird ohne die fehlenden Keys abgeschlossen und gedruckt (vorhandene werden vergeben), der Rest wird später unter ESD-Keys zugeordnet (Wunsch des Nutzers) |
| Vorläufige Belege | Was in der Belegmaske **neu entsteht** (Neuer Beleg, Übernehmen/Umwandeln, Kopieren als, Gutschrift, Sammelrechnung) ist **vorläufig** und hat **keine Nummer**, bis „Speichern: abschließen & Nummer vergeben“ es übernimmt. Beim Verlassen des Belegs fragt ein Modal: Abschließen & Nummer vergeben / Verwerfen / Zurück zum Beleg. Ein Hinweisbalken wird nicht angezeigt (Wunsch des Nutzers); die Marke „nicht gespeichert“ steht am Titel. Wird der Tab oder Browser geschlossen, wird der Beleg **sofort** verworfen: die Maske meldet das Schließen, der Befehl `belege_verwerfen` (Cron jede Minute, zusätzlich beim Öffnen der Start- und Kundenseite) verwirft ihn nach etwa 30 bis 90 Sekunden; Neuladen (F5) und ein zweiter offener Tab halten ihn. Die offene Maske sendet alle 15 Sekunden ein Lebenszeichen; bleibt es **60 Minuten** aus (Absturz, Ruhezustand, Netz weg), wird ebenfalls verworfen. Verwerfen nimmt **alles zurück**, „als ob keine Bestellung, Lieferschein- oder Rechnungsübergabe stattgefunden hat“: Mengen, Positionen, Positionsnummern, Seriennummern, Versandkosten und der Zustand der Bestellung (auch der Verlaufseintrag der Übernahme verschwindet; das Änderungsprotokoll hält das Verwerfen fest), eine Umwandlung wird mit alter Art, Nummer, Datum, Gültigkeit und Status zurückgewandelt, ein mitentstandenes Angebot für Preislistenartikel verschwindet. Weil vorläufige Belege nie eine Nummer haben, entsteht keine Lücke in den Nummernkreisen. Eine Bestellung wird erst mit dem Abschluss des Folgebelegs erledigt; das P-Angebot bekommt seine Nummer ebenfalls erst dann. Belege mit Nummer, Altentwürfe, Shop-Importe, Abo-Bestellungen und Werkstatt-Rechnungen sind nicht vorläufig: Änderungen wirken sofort, nichts wird automatisch verworfen. Vor dem Speichern gibt es **gar nichts nach außen** (Knöpfe ausgeblendet und serverseitig gesperrt): kein PDF, Druck, keine Mail, Zahlung, kein Versandlabel, kein ESD-Key, keine Lieferantenbestellung, kein Abo, keine Werkstatt, Retoure oder XRechnung; Seriennummern sind erlaubt. Übernehmen oder Kopieren aus einem vorläufigen Beleg fragt „Erst abschließen & Nummer vergeben?“, schließt ab und übernimmt dann (nie Ketten vorläufiger Belege). **Ausnahme Angebot (Regel des Nutzers):** Aus einem nicht gespeicherten **Angebot** wird ohne Rückfrage übernommen oder kopiert (Als Bestellung übernehmen, Kopieren als Rechnung usw.; auch die Frage „Angebot offen lassen?“ entfällt). Das Angebot wird dabei nicht sofort nummeriert, sondern erst mit „Speichern: abschließen & Nummer vergeben“ des neuen Belegs – in derselben Transaktion zuerst das Angebot (Nummer aus dem AN-Nummernkreis, bei einer Umwandlung AN→AB im Verlauf „Angebot AN… → Bestellung AB…“), dann der neue Beleg; hat der Benutzer das Angebot inzwischen selbst gespeichert, geschieht nichts doppelt. Wird der neue Beleg verworfen (Knopf, Verlassen-Dialog, Schließen des Tabs), ist alles wie vorher: das Angebot bleibt nicht gespeichert, ohne Nummer, mit eigenem Vorgang, ohne Lücke im Nummernkreis. Solange der neue Beleg besteht, lässt sich das Angebot nicht verwerfen (Meldung „erst diesen speichern oder verwerfen“) und wird nicht von der Bereinigung entfernt; Bearbeiten und eigenes Speichern bleiben möglich. Rechnung/Gutschrift mit älterem Belegdatum bekommen beim Abschluss das Abschlussdatum, andere Belege behalten das Anlagedatum. Teillieferung aus einem vorläufigen Folgebeleg: Der lieferbare Teil wird abgeschlossen, der Rest geht sofort zurück in die Bestellung (kein Rest-Entwurf); ein neuer Beleg ohne Bestellung bleibt mit dem Rest vorläufig. Vorläufige Bestellung und Proforma reservieren Bestand (Schutz vor Doppelverkauf), zählen aber nicht im Dashboard. |
| Positionsimport | Angebot, Bestellung, Rechnung und Proforma: „Positionen aus Datei importieren“ (CSV, XLS, XLSX bis 5 MB, höchstens 1000 Zeilen). Pflicht sind die Überschriften nummer und menge, dazu benennung, netto, gesamtnetto, Zusatz_pos, Kennzeichen, Rabatt; die Spalte pos zählt nicht. Der Preis kommt aus der Zeile, ohne netto gilt der Kundenpreis; dieselbe Nummer darf mit mehreren Preisen vorkommen. Die Nummer muss angelegt und aktiv sein, keine Gruppe. gesamtnetto wird geprüft. Alles oder nichts; die Positionen werden angehängt und gelten wie jede Positionsänderung erst nach dem Speichern |
| Rechnung an Stichtagen | Rhythmus der wiederkehrenden Rechnung „an Stichtagen“: Die Tage im Monat sind je Abo **frei wählbar** (z. B. „10, 25“ oder „1, 15, letzter“; Vorgabe und bisheriger Stand „15, letzter“; ein Tag über der Monatslänge zählt als Monatsletzter). Zu jedem Stichtag entsteht eine Bestellung (mit Nummer, nie automatisch verworfen) für den Zeitraum seit dem vorigen Stichtag, Zeitraum als Zusatztext je Position. Mengenregel je Position: feste Menge, Wochentage (Mo–Fr ohne Feiertag), Wochenende und Feiertage, alle Kalendertage. Feiertage sind die gesetzlichen in NRW, ein Feiertag am Wochenende zählt einmal. „Nur am Monatsende“ rechnet über den ganzen Kalendermonat und steht in der Bestellung, in deren Zeitraum der Monatsletzte fällt (Wunsch des Nutzers). Das Belegdatum liegt nie vor dem Stichtag, ein versäumter Termin rechnet seinen Zeitraum ab. Die Bestellung entsteht vorab mit dem Stichtag als Datum; **in eine Rechnung übernehmen geht erst ab dem Stichtag** (Entscheidung 19.11.2026: keine Rechnung und keine Gutschrift mit einem Datum in der Zukunft – speichern erst an diesem Tag; ein früheres Datum wird beim Speichern auf heute gesetzt). Das Ende muss ein Stichtag sein, mit Positionen „nur am Monatsende“ einer, dessen Zeitraum einen Monatsletzten enthält. Bei monatlich, vierteljährlich usw. ist der „Tag im Monat“ in der Verwaltung änderbar und verschiebt den nächsten Termin. Einrichten aus einer Rechnung (auch Entwurf): Das Programm schlägt die Regeln aus Zeitraum im Zusatztext und Menge vor, Monatszeitraum mit fremder Menge wird „feste Menge, nur am Monatsende“; Preise und Regeln pflegt man in der Verwaltung. Nicht aus Rechnungen mit Preislistenartikeln oder Gruppen mit Lagerartikeln |
| Laufende Nummern | Verwaltung → Laufende Nummern stellt die nächste Nummer ein, das Format bleibt. Belegnummern zählen je Mandant und laufen über den Jahreswechsel weiter, nur die Jahreszahl in der Nummer ändert sich. Vergebene Nummern kommen nie wieder: Die nächste muss über der höchsten vergebenen liegen; dazu zählen auch die früheren Nummern umgewandelter Belege (Angebot, Bestellung). Eine umgewandelte Rechnung bekommt ihre Nummer erst beim Abschließen. Rechnungen und Gutschriften sind lückenlos und nur einstellbar, solange im laufenden Jahr noch keine Nummer vergeben wurde (maßgeblich ist der Tag der Vergabe, nicht die Jahreszahl in der Nummer). Artikel (A, L, G), Kunden und Lieferanten gelten für beide Mandanten, haben je einen Zähler und überspringen belegte Nummern; eine abgebrochene Maske verbraucht keine Nummer, Shop-Kunden nutzen den Kundenzähler |
| Positionsnummern | lückenlos 1, 2, 3 …, nach Löschen automatisch neu vergeben; Gruppen-Unterpositionen tragen die Nummer ihrer Gruppe |
| Gruppe (Typ G) | Komponenten pflegt man in der Artikelmaske (Reiter „Komponenten“, auch beim Neuanlegen): Lagerartikel und Leistungen, keine Gruppen. EK der Gruppe = Summe der Komponenten, Leistungen zählen mit ihren Kosten, ohne Kosten mit 0 €; Marge in % vom VK. Im Reiter lässt sich der Gruppenpreis oder pauschal ein Aufschlag auf den EK setzen und in VK1–VK3 übernehmen. Die Artikelseite zeigt Komponenten mit EK, VK, Marge und den Gruppenpreis. Eine Position kommt nur in eine Gruppe mit demselben Steuersatz, denn versteuert wird der Gruppenkopf |
| Leistung (Typ L) | Dienstleistung, keine Ware: kein Lagerbestand, keine Lieferantenbestellung, kein Wareneingang, keine Maße, kein Gewicht, nicht im Shop. In Belegen ganz normal berechenbar. Eigene Leistungskategorien ohne Verbindung zu den Artikelkategorien. „Kosten (EK)“ ist der Aufwand je Einheit, gilt für beide Mandanten und zählt in Belegen und Gruppen als EK für die Marge; ohne Kosten bleibt der EK im Beleg unvollständig. Kosten ändern keinen Preis: Für Leistungen gelten nur die fest eingetragenen VK, kein Aufschlag auf den EK. Anlegen und Import: Verwaltung → Leistungsartikel |
| Lieferantenbestellung aus Kundenbeleg | „In Lieferantenbestellung kopieren“ legt je Lieferant eine Entwurfsbestellung an und merkt den Kundenbeleg und den Benutzer. Als bestellt gilt eine Menge erst ab Status „bestellt“; Entwürfe und Stornos zählen nicht. Bestellungen aus dem Angebot zählen auch für die daraus kopierte oder umgewandelte Kundenbestellung. Ist aus einer **Kundenbestellung** schon etwas bestellt, fragt das Programm vor dem erneuten Kopieren: „Nur Fehlendes bestellen“ (Restmengen) oder „Trotzdem komplett neu bestellen“ (Grund Pflicht, steht in der Bemerkung). Angebote werden ohne Rückfrage kopiert. Angebot und Kundenbestellung zeigen alle Lieferantenbestellungen (auch Entwürfe und Stornos) und je Position „bestellt: x von y“ (Issue 61). **Lieferantenwahl (Wunsch des Nutzers 16.11.2026):** Hat der Kundenbeleg mindestens zwei zu bestellende Positionen und führen mindestens zwei Lieferanten eine davon, fragt das Programm vor dem Anlegen, bei wem bestellt wird. Die Seite vergleicht den Warenwert (EK netto × Menge aus den Preislisten, ohne Versandkosten und Mindestbestellwerte): „Günstigste Aufteilung“ (je Position der günstigste Lieferant, Bestand 0 nur, wenn sonst keiner liefert) und „Alles bei …“ je Lieferant mit Unterschied in Euro; führt ein Lieferant eine Position nicht, kommt sie vom günstigsten anderen. Vorbelegt ist die günstigste Aufteilung, die Wahl ist je Position änderbar; angelegt wird je gewähltem Lieferanten eine Entwurfsbestellung mit dessen Preis und Bestellnummer. Preislistenartikel (Typ P) lassen sich bei einem anderen Lieferanten mit derselben Herstellernummer bestellen; das zählt für die Position als bestellt. Gesperrte Lieferanten fehlen. Bei nur einer Position oder nur einem Lieferanten bleibt es ohne Frage beim günstigsten Lieferanten (Typ P: dem der Preislistenzeile) |
| Lieferantenbestellung vorläufig (Entscheidung 19.11.2026) | Wie die Kundenbestellung im Verkauf: „+ Bestellung“ beim Lieferanten (und in der Bestellliste) und „In Lieferantenbestellung kopieren“ legen eine **vorläufige** Bestellung **ohne Nummer** an (Marke „nicht gespeichert“, Titel „Bestellung (nicht gespeichert)“; `Bestellung.vorlaeufig`, `zuletzt_aktiv`, `nummer` leer erlaubt). Die Nummer (Kreis BE) vergibt erst „Speichern: abschließen & Nummer vergeben“ (Status bleibt „Entwurf“, bis bestellt wird); ohne Position nicht möglich, doppelt geklickt nur einmal (Zeile gesperrt), die Kreise bleiben lückenlos. Beim Verlassen fragt ein Fenster: Abschließen / Verwerfen / Zurück. Verwerfen löscht die Bestellung samt Positionen (nie mit Wareneingang). Die offene Seite sendet alle 15 Sekunden ein Lebenszeichen; wird der Tab geschlossen, verwirft das Öffnen der Bestellliste oder der Lieferantenseite sie nach etwa 30 bis 90 Sekunden, ohne Lebenszeichen nach **60 Minuten**. „Per E-Mail bestellen“, „Als bestellt markieren“, PDF und „Wareneingang erfassen“ fragen bei einer vorläufigen Bestellung erst „Erst abschließen & Nummer vergeben?“ (Bestätigen schließt ab und führt die Aktion aus). Die Tabs (Positionen, Bedarf, Versand & Konditionen, Texte) arbeiten normal, jede Änderung zählt als Lebenszeichen. Ergibt „In Lieferantenbestellung kopieren“ mehrere Bestellungen (mehrere Lieferanten), bekommen alle sofort ihre Nummer, ebenso Bestellvorschlag und Lieferantenvergleich (das Programm legt sie in einem Rutsch an). Eine Bestellung ohne Position, die schon eine Nummer hat, wird wie bisher nach ein paar Minuten entfernt und die Nummer wieder frei |
| Status-Symbole | Status in Listen als Symbol (zentrale Tabelle `apps/kern/templatetags/status.py`, Tag `{% status_symbol %}`): gleiche Bedeutung = gleiches Symbol, Text als Tooltip und für Screenreader, im Detail Symbol und Text, beim Drucken Text. Offene Rechnungen mit überschrittener Fälligkeit erscheinen rot (überfällig), läuft die Skontofrist einer offenen Lieferantenrechnung in 2 Tagen ab, steht ein %. ⓘ im Tabellenkopf zeigt die Legende (Issue 62) |
| Teillieferung | Fehlt beim Wareneingang eine Position ganz oder teilweise: Menge verringern bzw. × „nicht geliefert“ (Menge 0 wirkt genauso). Gebucht wird nur die gelieferte Menge, der Rest bleibt in der Lieferantenbestellung offen (Status „teilweise geliefert“) und wird am Wareneingang als „bleibt offen“ angezeigt; der nächste Wareneingang aus der Bestellung übernimmt nur den Rest. „doch geliefert“ nimmt eine entfernte Position wieder auf |
| VK-Vorschlag | Im Artikel (Verwaltung und Dialog) zeigt ein leeres VK-Feld den Preis aus EK und Aufschlag (Artikel vor Kategorie vor global) und bleibt änderbar. Bleibt der Vorschlag unverändert, wird kein fester Preis gespeichert und der VK folgt weiter dem EK; ein eingetippter Preis ist fest, Leeren holt den Vorschlag zurück. Das Häkchen „VK1 für alle VKs übernehmen“ schreibt VK1 fest in VK1 bis VK3 |
| Preisanzeige | Alle Preise mit 2 Stellen. EK in Bestellung und Wareneingang wird auf 2 Stellen gerundet, damit EK × Menge aufgeht; Preisliste und Import-EK rechnen intern mit 4 Stellen |
| Kalkulations-EK | Lieferanten-EK plus anteilige Nebenkosten des Wareneingangs (Versand, Versicherung, Verpackung), verteilt nach Warenwert. Er wird an der Wareneingangsposition festgehalten und ändert den EK des Artikels nicht (maßgebend ist der Import-EK). „Letzter EK“ bleibt der Lieferantenpreis (Vorschlag für Bestellungen). Der Abgleich zur Lieferantenrechnung rechnet die Nebenkosten mit, außer sie sind als „auf eigener Rechnung“ markiert (z. B. Spedition bei Auslandsware); diese Rechnung wird ohne Wareneingang erfasst |
| EK (errechnet) | Maßgebend ist der Import-EK: zuerst der übernommene EK (Zeitplan „Günstigster EK“, Preis-Assistent), sonst der günstigste Preislisten-EK, sonst der beim Anlegen eingetragene „EK-Preis netto“ (manuell, nur Typ A, immer änderbar). Wareneingang und Lagerbestand ändern den EK nicht. Preisimport und Bestpreis-Übernahme setzen die Herkunft auf „Import“ mit Datum (Herkunft „Wareneingang“ nur noch bei Altdaten ohne Preisliste). Derselbe EK gilt für VK per Aufschlag, Marge und Lagerwert. Ohne EK zeigen Artikelliste und Artikelseite „EK fehlt“; Gruppenartikel (G) = Summe der Komponenten; Leistungen (L) = eingetragene Kosten. Grundlage für VK per Aufschlag; Leistungskosten zählen dabei nicht, auch nicht in Gruppen |
| Kalkulations-EK je Mandant (Issue 12, löst „Import-EK ist maßgebend“ ab, Stand Schritt 1) | Pro Artikel (Typ A) und Mandant: `ek_avg` (gleitender Ø-EK), `ek_we` (letzter Wareneingang), `ek2` mit `ek2_datum` und `ek2_fix`; `ek_import` ist der günstigste Preislisten-EK und gilt für alle Mandanten. Ø-EK bei jedem Wareneingang: (Bestand alt × Ø-EK alt + Menge × Preis) / (Bestand alt + Menge), bei Bestand ≤ 0 gleich dem Preis; Preis ist der Kalkulations-EK der Position (mit Nebenkosten); Verkäufe, Abgänge und Retouren ändern ihn nicht; 4 Nachkommastellen. `ek_kalk`, erster Treffer: ek2_fix → ek2; Bestand > 0 → max(Ø-EK, ek_import); Bestand 0 → ek_import, dann ek_we, dann ek2; sonst kein EK und kein VK. VK = ek_kalk / (1 − Marge); die Marge ist der bisherige Aufschlag umgerechnet (Marge = Aufschlag / (100 + Aufschlag)); Rundung 2 Stellen oder aufgerundet auf ,99 (globale Preisvorgabe). Neuer VK: Erhöhung automatisch; Senkung bis 10 % automatisch, darüber nur per Klick (Liste „VK-Senkung prüfen“); `vk_fix` je Stufe ändert nie. Gruppen (G) und Leistungen (L) behalten ihre Regel. Schutz (Schritt 2): Im Wareneingang Rückfrage vor dem Buchen, wenn der Kalkulations-EK um mehr als 50 % vom Ø-EK (nur mit Bestand) bzw. Import-EK abweicht. Preisimport: ändern sich mehr als 20 % der Artikel (mindestens 5 mit bisherigem Preis) um mehr als 30 %, wird der gesamte Import zurückgehalten (Status „zur Prüfung“, Freigeben oder Verwerfen); ein einzelner Preis mit mehr als 50 % Abweichung zum bisherigen wird nicht übernommen, sondern in die Prüfliste gestellt (Übernahme per Klick). Grenzen in der globalen Preisvorgabe. Preis-Radar (Schritt 3, je Mandant, Dashboard-Kachel und Liste „Preis-Radar“): Vergleichsbasis ist der Ø-EK (Bestand > 0), sonst ek_kalk. Kategorien: Verlust (VK < Basis, rot), Marge unter Ziel (Zielmarge VK1, gelb), Preisverfall (Import-EK < Ø-EK × 0,85, blau), Preiserhöhung möglich (nur bei `vk_fix`, grün), EK2 prüfen (älter als 180 Tage), EK-Abweichung (> 20 % zu EK2), Kein EK (grau). Euro-Wert = (Bestand + offene Bestellungen + offene Angebote) × Differenz je Stück, Liste absteigend. Im Dashboard nur mit Menge > 0; „EK2 prüfen“ und „Kein EK“ nur als Ampel in der Artikelliste. Differenzen unter 3 % der Basis oder unter 0,50 € je Stück werden nicht gemeldet; Typ P ohne Menge nie, Typ L nur mit Kosten, Typ G nie; `warnungen_aus` zeigt nur die Ampel. „Gesehen“ blendet aus, bis sich ek_kalk, Ø-EK oder VK spürbar ändern. Liste „VK-Senkung prüfen“ (Übernahme per Klick, nur Admin). Noch nicht angeschlossen: Artikelmaske, automatische VK-Übernahme, EK-Trend, Belegwarnung, Rohertrag, Mail (Schritte 1b, 4) |
| Zahlungen | Eingang und Ausgang sind unveränderlich; Korrektur nur per Stornobuchung mit Storno-Grund |
| Artikeldaten an den Shop | Online-Artikel (A, G) gehen per Knopf „Artikeldaten senden“ (auch nur eine Artikelnummer) oder im Takt aus Verwaltung → Shops an den Shop; Standard ist „nur per Knopf“. Die Zuordnung läuft über die Artikelnummer. In wwok leere Felder überschreiben im Shop nichts, gefüllte schon. Bestand und Preise (Standardpreis und Preis für angemeldete Kunden) gehen dabei mit, nach denselben Regeln wie bei „Bestand & Preise senden“; ohne „alle Artikel“ nur, wenn sie sich seit der letzten Meldung geändert haben. Bei Gruppenartikeln (G) hängt wwok an die Beschreibung die Liste „Bestandteile“ mit Menge und Bezeichnung jeder Komponente an, ohne Einzelpreise; ändert sich die Zusammensetzung, wird der Artikel neu gesendet. Fehlt der Artikel im Shop, wird er sofort sichtbar angelegt und ist nur bis zum gemeldeten Bestand bestellbar. Gruppenartikel (G) sind im Shop dagegen immer bestellbar, unabhängig vom Bestand der Komponenten (auch bereits angelegte, beim nächsten Senden); ohne Preis oder mit 0 € wird er nicht angelegt. Knopf nur für Admins; ein Lauf per Knopf endet nach 12 Sekunden, der nächste macht weiter. Die Kategorie wird zugeordnet oder bei Bedarf angelegt (siehe „Shop-Kategorien (Issue 59)“), fehlende Hersteller werden angelegt. Wechselt die Kategorie, entfernt wwok nur die selbst gesetzte alte. **Weitere Kategorien** (Entscheidung 19.11.2026) gelten auch im Shop: Der Artikel wird in allen seinen wirksam online Kategorien (Haupt- und weitere) als Shopware-`categories` gesendet, offline Kategorien werden nicht übertragen; entfällt eine weitere Kategorie, entfernt wwok sie im Shop wieder (nur die selbst gesetzten). Im Shop sichtbar ist ein Artikel, wenn er online und aktiv ist und mindestens eine seiner Kategorien wirksam online ist (oder er gar keine hat); ist nur die Hauptkategorie offline, eine weitere aber online, bleibt er sichtbar. **Grundpreis** geht als `purchaseUnit` = 1 / Faktor, `referenceUnit` = 1 und `unitId` mit, wenn es die Einheit im Shop gibt (Kurzzeichen oder Name, ohne Groß-/Kleinschreibung; sonst bleibt der Grundpreis weg und der Lauf meldet es). Die **UVP** (netto) geht als Listenpreis des Standardpreises (`price[].listPrice`, brutto mit dem Steuersatz des Artikels) mit Bilder: Das erste ist Titelbild; hat ein Produkt im Shop schon Bilder, die nicht von wwok stammen, bleiben sie und wwok sendet keine, damit nichts doppelt erscheint; gelöscht werden nur Bilder, die wwok selbst gesendet hat; fehlt eine Bilddatei, bleiben die Bilder im Shop unverändert. Fällt „online“ weg oder wird der Artikel „nicht aktiv“, setzt der nächste Abgleich ihn im Shop auf inaktiv, gelöscht wird nichts; betroffen sind nur Artikel, die wwok schon einmal gemeldet hat. Das gilt für „Bestand & Preise senden“ ebenso wie für „Artikeldaten senden“; mit einer Artikelnummer wird genau dieser Artikel im Shop deaktiviert, auch erneut, falls er dort von Hand wieder eingeschaltet wurde. Mehr als 50 auf einmal stoppen mit Meldung und gehen nur per „Bestand & Preise senden“ mit „alle Artikel“ |
| Shop-Preise | Der Abgleich meldet die Preisstufe des Shops als Standardpreis. Sind in Verwaltung → Shops eine Preisstufe für angemeldete Kunden und die Regel-ID aus dem Shopware Rule Builder eingetragen, geht diese Stufe zusätzlich als Preis mit Regel an den Shop, gilt ab Stück 1 und folgt dem VK; entfällt der VK, wird der Preis im Shop entfernt. Steht bei einem Produkt schon ein fremder Preis mit Regel, bleibt er unberührt, wwok sendet dort keinen Kundenpreis und nennt das im Protokoll. Gibt es die Regel im Shop nicht, gehen Bestand und Standardpreis trotzdem raus. Ob netto oder brutto angezeigt wird und wer unter die Regel fällt, legt Shopware fest (Kundengruppe „Anzeige“ und Regel). Beim Bestellimport zählt der Steuerstatus der Bestellung: Preise von Netto-Kunden werden unverändert übernommen, steuerfreie mit 0 % |
| Gutschrift verrechnen | Eine abgeschlossene Gutschrift mindert die Rechnung nicht von selbst, denn der Kunde kann schon bezahlt haben, ohne dass die Zahlung gebucht ist. Sie steht als eigener Minusposten in den offenen Posten, die Rechnung bleibt in voller Höhe offen und läuft normal in Mahnung und Lastschrift. Verrechnet wird von Hand auf der Zahlungsseite einer Rechnung desselben Kunden, mit frei wählbarem Betrag bis zum Rest der Gutschrift; ein Rest bleibt als Guthaben stehen. Die Rechnung darf dabei ins Minus gehen, die Überzahlung wird als Rückzahlung (negative Zahlung) gebucht. Verrechnungen sind unveränderlich; Admins heben sie per Gegenbuchung mit Pflichtgrund auf. Kontoauszug und Kreditlimit rechnen den Saldo, unverrechnete Gutschriften zählen dort also mit. Alle vor dieser Regel entstandenen Gutschriften gelten als unverrechnet |
| Abgeschlossene Rechnung/Gutschrift | gesperrt, Korrektur nur per Gutschrift |
| Shop-Kategorien (Issue 59) | Beim Senden eines Online-Artikels wird seine Kategorie im Shop zugeordnet: zuerst über die gespeicherte Zuordnung, sonst über den Pfad ab der Hauptnavigation des Verkaufskanals (Hauptkategorien wie bisher auch über einen eindeutigen Namen). Fehlt sie, wird sie samt fehlender Elternkategorien unter der Hauptnavigation angelegt (feste ID, nichts doppelt). Nur wirksam online Kategorien und nur, wenn ein Online-Artikel gesendet wird; bei mehrdeutigem Pfad wird nichts angelegt. Umbenennen und Verschieben in wwok werden nur für Kategorien nachgezogen, die wwok selbst angelegt hat; fremde Shop-Kategorien bleiben unberührt, gelöscht wird im Shop nichts. Die Reihenfolge im Menü steuert der Shop. Verwaltung → Kategorien zeigt je Shop „angelegt“ oder „zugeordnet“. Shops → „Kategorien zuordnen“ (nur Admin) vergleicht alle wirksam online Kategorien mit dem Shop: zugeordnet, im Shop vorhanden, fehlt (wird beim Senden angelegt) oder im Shop mehrfach vorhanden. Bei mehrfachen wählt der Admin die richtige Shop-Kategorie aus oder trägt ihre ID ein; danach gilt immer diese ID, auch für die Unterkategorien darunter. Eine Shop-Kategorie kann nur einer Kategorie zugeordnet sein. „Vorhandene jetzt zuordnen“ ordnet alle im Shop eindeutig vorhandenen Kategorien auf einmal zu, ohne Artikel zu senden und ohne im Shop etwas anzulegen oder zu ändern |
| Bankabgleich | Eingänge mit Rechnungs- oder Proformanummer im Verwendungszweck werden beim Import gebucht, solange sie die offenen Beträge nicht übersteigen: genau passend, als Teilzahlung oder bei mehreren Nummern der Reihe nach verteilt (Wunsch des Nutzers). Eine Überzahlung bleibt Vorschlag und wird von Hand gebucht; dort bleibt der Rest als Guthaben auf der letzten genannten Rechnung. Jede Zahlung kennt ihren Bankumsatz; ein Storno gibt den Umsatz erst frei, wenn keine seiner Zahlungen mehr gilt |
| Überzahlung verrechnen | Hat der Kunde eine Rechnung überzahlt (oder bleibt Guthaben aus Proforma/Bestellung), schlägt die Zahlungsseite jeder offenen Rechnung desselben Kunden diese Guthaben unter „Guthaben des Kunden verrechnen“ vor (Betrag höchstens offen, nichts automatisch). Die Verrechnung ist unveränderlich, steht auf beiden Belegen („Weiterverrechnet an“ / „Erhalten aus“) und als Zeile im Kundenkontoauszug (ändert den Saldo nicht); Korrektur nur per „Aufheben“ mit Grund. Verrechnetes Guthaben lässt sich erst nach dem Aufheben zurückzahlen. Ausdruck und E-Rechnung der Zielrechnung bleiben unverändert (GoBD) |
| Proforma-Rechnung (Vorkasse, Issue 57) | Entsteht **nur aus einer Bestellung** (Mehr → Kopieren als → Proforma-Rechnung) und hat immer die Zahlungsbedingung **Vorkasse**, egal was beim Kunden hinterlegt ist. Sie nimmt die Menge aus der Bestellung (die Bestellung behält nur den Rest; ihre Reservierung geht auf die Proforma über). Nach dem Abschließen nimmt sie Zahlungen an (auch Bankabgleich über die PF-Nummer): Status offen → teilbezahlt → bezahlt; Überzahlung ist erlaubt (Guthaben). Mit der ersten Zahlung sind Positionen und Beträge fest. **Erst wenn sie vollständig bezahlt ist**, wird sie per „In Rechnung umwandeln“ zur abgeschlossenen Rechnung mit denselben Positionen und der Zahlungsbedingung „Betrag dankend erhalten“; „Kopieren als Rechnung“ ist gesperrt. Die Zahlung bleibt mit Datum, Zahlart, Bank und Kasse an der Proforma und wird mit der Rechnung **verrechnet** (unveränderliche Anzahlungsverrechnung, Aufhebung nur per Gegenbuchung mit Grund), nicht umgebucht: kein zweiter Kassen- oder DATEV-Eintrag. Ein Guthaben wird zurückgezahlt (negative Zahlung) oder mit anderen Rechnungen desselben Kunden verrechnet. Zur Rechnung lässt sich ein Lieferschein ohne Preise drucken, ohne zweiten Lagerabgang. Kauft der Kunde nicht: erst den Betrag zurückzahlen, dann die Proforma **stornieren**; dabei entsteht ein eigener, unveränderlicher Storno-Beleg (Belegart SP, eigene Nummer, Grund in der Bemerkung), die Proforma ist danach gesperrt und ihre Menge steht wieder in der Bestellung (der Storno bucht sie zurück, die erledigte Bestellung ist wieder offen). Die Proforma ist keine Forderung und kein Umsatz: nicht in der Summe der Offenen Posten (eigener Abschnitt „Offene Proformas“), nicht im Mahnwesen, in der Lastschrift, im Kreditlimit, im Umsatz und nicht als DATEV-Belegzeile. Zahlungen auf eine Bestellung (Shop/PayPal) werden beim Abschluss der Rechnung genauso verrechnet. Gutschrift zur Rechnung nach Verrechnung: die Erstattung läuft über die Gutschrift, die Verrechnung bleibt. USt auf Anzahlungen und DATEV-Konto für erhaltene Anzahlungen klärt der Steuerberater; bis dahin exportiert DATEV nur die Zahlung |
| Gutschrift und Ware | Beim Anlegen zu einer Rechnung mit Lagerartikeln fragt das Programm: Reparaturbestand, Lagerbestand oder keine Ware zurück. Spalte B zeigt das je Lagerartikel (R, B, –), ein Klick wechselt R → B → –. Gebucht wird beim Abschließen der Gutschrift, einmal. Über alle Gutschriften einer Rechnung darf je Artikel mit R oder B höchstens die Rechnungsmenge zurückgebucht werden, sonst sperrt das Abschließen; „–“ und freie Gutschriften ohne Rechnung sind davon frei. Ändert man die Menge eines Gruppenkopfs, folgen die Unterpositionen im selben Verhältnis. Eine freie Gutschrift beginnt mit R, neue Positionen folgen der ersten Position. Vor dieser Regel entstandene Gutschriften bucht man weiter von Hand. Seriennummern ändert die Gutschrift nicht: Am Artikel holt „Wieder einlagern“ eine verkaufte Nummer eines abgeschlossenen Belegs zurück, ohne den Bestand zu ändern |
| Reparaturbestand | Eigener Bestand je Mandant, getrennt vom Lagerbestand: nicht verkaufbar, nicht im Lagerwert, nicht in der Inventur, nie unter 0, Bewegungen unveränderlich. Er verlässt den Bestand per „In Lagerbestand umbuchen“ oder per „Ausbuchen“ mit Pflicht-Bemerkung (Schrott, Rückgabe an den Lieferanten) |
| Nach Abschließen, PDF oder E-Mail | die Anwendung kehrt zur Belegübersicht des Kunden zurück |
| Gutschrift (Entscheidung 19.11.2026) | zu einer Rechnung aus der abgeschlossenen Rechnung (Mehr → „Storno / Gutschrift“); **ohne Rechnung** unter der Kundenadresse mit „+ Gutschrift“ (z. B. Kulanz, Preisnachlass). Nicht per Kopieren. Die freie Gutschrift hat Positionen wie eine Rechnung, Spalte B wie jede Gutschrift (Standard „keine Ware zurück“, R/B bucht Reparatur- bzw. Lagerbestand), einen **Pflichtgrund**, der gedruckt wird, und einen frei wählbaren Bezug unter „Bezugnahme“. Nach dem Abschluss ist sie Guthaben des Kunden in den Offenen Posten und wird wie jede Gutschrift verrechnet oder ausgezahlt. **Auszahlung:** Jede Gutschrift lässt sich auf ihrer Zahlungsseite direkt an den Kunden auszahlen (negative Zahlung auf der Gutschrift, höchstens der noch freie Betrag; bar nur bei ausreichendem Kassenstand; Storno per Gegenbuchung) – auch ohne Rechnung des Kunden |
| Gutschrift über dem Rechnungsbetrag | Beim Abschließen werden alle abgeschlossenen Gutschriften der Rechnung zusammengezählt (auch Teilgutschriften). Liegt die Summe mit der neuen Gutschrift über dem Rechnungsbetrag (brutto), sperrt das Programm nicht, sondern warnt und verlangt eine **Begründung** (Pflicht). Sie wird mit dem Benutzer an der Gutschrift gespeichert, nicht gedruckt und steht am Beleg und im Zahlungsweg. Schon der Entwurf zeigt die Warnung. Eine weitere Gutschrift zu einer bereits voll gutgeschriebenen Rechnung lässt sich weiterhin nicht anlegen (Schutz vor doppeltem Storno) |
| Zahlungsweg | Die Zahlungsseite von Rechnung und Gutschrift zeigt alle Schritte in zeitlicher Reihenfolge: Rechnung, Zahlungseingänge, Gutschriften, Verrechnungen, Auszahlungen an den Kunden und Stornos, jeweils mit Zahlart, Herkunft (Kontoauszug, Kassenbuch-Nummer, Lastschriftdatei), Benutzer, Bemerkung oder Grund und dem offenen Betrag der Rechnung nach dem Schritt. Eine Gutschrift wird mit einer Rechnung verrechnet (ein Guthaben der Rechnung dort als negative Zahlung ausgezahlt) oder auf ihrer eigenen Zahlungsseite direkt an den Kunden ausgezahlt (Entscheidung 19.11.2026) |
| Beleg kopieren | jeder Beleg lässt sich als beliebige Belegart (außer Gutschrift und Storno Proforma) kopieren; „Kopieren als …“ ist eine echte Kopie (Ausnahme: Bestellung → Lieferschein/Rechnung/Proforma, dort wandert die Menge aus der Bestellung), nur „Übernehmen“ wandelt um; eine Proforma nur aus einer Bestellung, eine Rechnung aus einer Proforma nur per „In Rechnung umwandeln“; beim Angebot wird gefragt, ob es danach abgeschlossen werden soll |
| Belegrabatt | Prozent je Beleg, wirkt auf alle Positionen netto vor der Steuer (je Steuersatz auf Cent gerundet); Versand, Verpackung und Versicherung bleiben unberührt. Wird in Folgebelege, Gutschrift, PDF, ZUGFeRD und DATEV übernommen; Sammelrechnung nur bei gleichem Rabatt |
| USt-ID-Prüfung | Reiter „Steuer & Bank“ bei Kunde und Lieferant, ab dem ersten Speichern. Ausländische EU-Nummern prüft das BZSt mit Name (Name 1 und 2), Ort, Straße und PLZ; dafür braucht der Mandant seine deutsche USt-ID. Deutsche Nummern prüft VIES, nur die Gültigkeit. Nummern außerhalb der EU sind nicht prüfbar. Ein Ergebnis trägt das Datum in die Maske ein (ungültig: leert es), festgehalten erst mit „Speichern“; das Ändern der Nummer leert das Datum. Weicht Name oder Anschrift ab, bleibt die Nummer gültig und das Programm warnt. Jede Antwort mit Aussage bleibt als unveränderlicher Nachweis erhalten (Verwaltung → USt-ID-Prüfungen, je Mandant), Störungen nicht |
| Lieferantenrechnung löschen | Nur offene Rechnungen, mit Pflichtnotiz. Die Zuordnung zum Wareneingang entfällt, der Wareneingang lässt sich neu verknüpfen, Lager und EK bleiben unverändert. Die Rechnungsnummer ist wieder frei. Notiz und Benutzer stehen im Änderungsprotokoll. Bezahlte Rechnungen erst nach dem Zahlungsstorno (Admin), exportierte (SEPA) nicht; ist eine exportierte Zahlung nicht ausgeführt worden, führt der Weg über Als bezahlt buchen und Zahlungsstorno |
| Lieferantenrechnung | Weicht das Netto vom Wareneingang um mehr als 1 Cent ab, wird es gemeldet; Zahlung und SEPA-Export sind gesperrt, bis die Beträge korrigiert oder die Abweichung mit Begründung bestätigt ist (Verlauf mit Benutzer und Zeit; ändert sich die Differenz, muss neu bestätigt werden). Dazu ein Notizfeld |
| Lieferanten-Gutschrift (Entscheidung 19.11.2026) | „Gutschrift erhalten“ an einer WE-Rücksendung erfasst die Belegdaten des Lieferanten (`Lieferantengutschrift`); ohne Rücksendung (Bonus, Preisnachlass) unter Einkauf → Lieferanten-Gutschriften mit denselben Pflichtangaben, dort auch die Liste mit Restguthaben: Gutschrift-Nr. (Pflicht, je Mandant und Lieferant eindeutig), Gutschriftdatum (Pflicht, nicht in der Zukunft), Netto, USt, Brutto (Netto + USt = Brutto auf 1 Cent), Notiz; `Ruecksendung.gutschrift_betrag` bleibt das Netto. **Verrechnen** (auch teilweise, auch auf mehrere Rechnungen) mit offenen Lieferantenrechnungen desselben Lieferanten und Mandanten: Betrag höchstens Restguthaben und offener Betrag der Rechnung (Brutto minus Verrechnungen); nicht bei bezahlten oder schon per SEPA exportierten Rechnungen, nicht bei offener Abweichung zum Wareneingang. Der Zahlbetrag der Rechnung (SEPA-Export, „Als bezahlt buchen“, Listen) sinkt um die Verrechnungen, nie unter 0; das Skonto bezieht sich auf das Rechnungsbrutto (Zahlbetrag = Brutto nach Skonto − verrechnet). Voll verrechnet = bezahlt (bezahlt am = Verrechnungsdatum, Zahlbetrag 0, Historie). Läge der Rest einer Rechnung unter dem Skontobetrag, muss der volle offene Betrag verrechnet werden. **Rückgängig** nur Admin, mit Pflichtgrund, nur solange die Rechnung offen oder allein durch Verrechnung bezahlt ist (nicht nach SEPA-Export, nicht nach gebuchter Zahlung); die Zeile bleibt als „rückgängig gemacht“ stehen, Historie an Gutschrift und Rechnung; eine Rechnung mit Verrechnungen (auch rückgängig gemachten) lässt sich nicht löschen, ihr Brutto nicht unter die Verrechnungen korrigieren. Das **Restguthaben** (Brutto − verrechnet − erhaltene Zahlungen) steht in Offene Posten Lieferanten und Zahlungen Lieferanten; **Zahlung erhalten buchen** (Datum, Betrag bis Restguthaben) erscheint unter „Zahlungseingänge von Lieferanten“, Storno nur Admin mit Pflichtgrund. **Bankabgleich:** Ein Eingang ohne Kundenrechnungsnummer, dessen Betrag genau dem Restguthaben einer offenen Gutschrift entspricht, wird automatisch gebucht, wenn die Gutschrift-Nr. als ganzes Wort im Text steht (nur ein Treffer); passt nur Name oder Kurzname des Lieferanten, bleibt es ein Vorschlag, den man in der Zeile „Auf Lieferanten-Gutschrift buchen“ von Hand bucht (Betrag bis Restguthaben); es ist dieselbe Zahlung wie der Knopf, ein Storno gibt den Umsatz wieder frei |
| Menü | Verkauf, Einkauf, Artikel (früher „Lager“), Werkstatt, Finanzen |

## 19. Betrieb: Datenimporte, Zeitpläne, Geheimnisse, Tests

Protokoll: Alles geht mit Zeit, Stufe und Name auf die Konsole (`journalctl -u wwok`), auch `django.security` (fremde Hosts, CSRF) und fehlgeschlagene Anmeldungen (Logger `wwok.anmeldung`, nur Benutzername und IP, nie das Passwort). Empfehlung (nicht Teil der Software): in nginx `limit_req` für `/konto/login/` setzen.

### 19.1 Datenimporte (Verwaltungsbefehle)

Aufruf auf dem Server als Systembenutzer, im Ordner `app`:

```bash
sudo -u shopokaycomputer ../venv/bin/python manage.py <befehl> <datei>
```

| Befehl | Zweck |
| --- | --- |
| `import_kategorien` | Kategorien aus der Mention-Tabelle |
| `import_zahlungsbedingungen` | Zahlungsbedingungen; `ff.ff.ffff` = Belegdatum plus Frist, intern = Mahnfälligkeit |
| `import_hersteller` | Hersteller mit Online-Kennzeichen |
| `import_artikel` | Artikelstamm (`.xls`) |
| `import_kunden` | Kundenstamm (`.xls`) |
| `import_bestand` | Anfangsbestände je Mandant |
| `preislisten_abrufen` | Lieferanten-Preislisten laut Profil abrufen und einlesen (Cron, stündlich) |
| `p_artikel_zuruecksetzen` | alle Preislistenartikel eines Lieferanten löschen, danach neu einlesen |
| `shop_sync` | Abgleich mit den Shopware-Shops (Cron, alle 10 Minuten) |
| `belege_verwerfen` | verwirft nicht gespeicherte (vorläufige) Belege, deren Maske geschlossen wurde; ohne `--ausfuehren` nur Probelauf (Cron, jede Minute) |
| `wiederkehrende_rechnungen` | legt zu allen fälligen wiederkehrenden Rechnungen eine Bestellung an (Cron, täglich) |
| `rueckstand_umstellen` | stellt Bestellungen mit alten Teilübernahmen auf die offene Restmenge um; ohne `--ausfuehren` nur Probelauf (einmalig für Altdaten) |
| `internetmarke_pruefen <Mandant>` | prüft den INTERNETMARKE-Zugang eines Mandanten (Anmeldung, Guthaben, Produktcodes), ohne Kauf |

Offene Posten aus Mention werden **nicht** importiert.

### 19.2 Preislisten-Import

- Einrichtung über Einkauf → „Preislisten & EK-Import“ mit dem **Assistenten** (Datei hochladen oder Abruf per FTP/SFTP/URL, Format wird erkannt, Spalten werden zugeordnet, Vorschau vor dem Import).
- Für P-Artikel genügen: Bestellnummer, Herstellernummer (MSKU), Bezeichnung, Hersteller und EK; EAN, Bestand und Lieferzeit sind optional.
- Ein gesperrter Lauf (`laeuft_seit`) verhindert, dass Cron und manueller Start gleichzeitig laufen. Der Import ist für über 140.000 Zeilen ausgelegt (Lauf nach dem ersten Mal etwa 20 Sekunden).

### 19.3 Geheimnisse und Rechte

- Passwörter und Schlüssel stehen in `.env` auf dem Server (Abschnitt 8.5), Rechte `chmod 600`. Ausnahme: das SMTP-Passwort je Mandant, verschlüsselt in der Verwaltung.
- Wurde ein Passwort versehentlich ausgegeben oder in einen Chat kopiert: **sofort ändern**.
- Shopware-Integration (Zugang je Shop in Verwaltung → Shops): braucht Lese- und Schreibrecht auf Produkte, Hersteller, Medien, Preise, Leserecht auf **Maßeinheiten** (`unit`, für den Grundpreis) und **Kategorien** (`category`, für das Anlegen fehlender Kategorien, Issue 59) sowie Leserecht auf Verkaufskanäle.

### 19.4 Tests

```bash
python manage.py test apps
```

Vor jedem Commit ausführen; bei Fehlern nicht deployen.

### 19.5 Typische Stolpersteine

| Problem | Lösung |
| --- | --- |
| Lokal Weiterleitung auf HTTPS (Status 301) | `DJANGO_DEBUG=true` in der lokalen `.env` |
| `OperationalError: no such column` lokal | `python manage.py migrate` |
| Preislisten-Abruf: 401 | Schlüssel in `.env` muss `PREISLISTE_PW_<Nummer des Profils>` heißen, nicht mit Platzhalter `<ID>` |
| Preislisten-Abruf: „unknown url type“ | Adresse im Profil vollständig mit `https://` oder `ftp://` eintragen |
| `venv/bin/python: command not found` auf dem Server | der Pfad ist `../venv/bin/python` relativ zu `app/` |

### Nachlieferschein

Zu einer abgeschlossenen Rechnung: **Mehr → Kopieren als → Nachlieferschein**. Eigener Nummernkreis `NL<Jahr>-<Nr.>` (beim Abschließen vergeben), Zahlungsbedingung „Betrag dankend erhalten“ (wird bei Bedarf angelegt), keine Versandkosten, beim Abschließen wird gefragt, ob die Ware vom Lager abgebucht wird: **ja** bei Ersatz für defekte Ware (Bestand wird geprüft, nie unter 0), **nein** bei bloßer Nachlieferung (schon mit Rechnung/Lieferschein abgebucht). Bis zur Antwort ist kein Druck möglich. Nicht über „Neuer Beleg“ anlegbar.

### Proforma-Rechnung, wiederkehrende Rechnungen, XRechnung

- **Proforma-Rechnung:** Belegart `PF` mit eigenem Nummernkreis, entsteht nur aus einer Bestellung (Mehr → Kopieren als …) und hat immer Vorkasse. Kein Lagerabgang und keine Forderung; das PDF trägt den Hinweis „keine Rechnung im Sinne des UStG“ und die Nummer als Verwendungszweck. Zahlungen werden auf die Proforma gebucht; vollständig bezahlt wird sie per „In Rechnung umwandeln“ zur Rechnung („Betrag dankend erhalten“, PDF und ZUGFeRD zeigen die erhaltene Zahlung). Storno über einen eigenen Beleg `SP`. Regeln: Abschnitt 18.
- **Wiederkehrende Rechnungen:** In einer Rechnung Mehr → „Wiederkehrend einrichten“ (Rhythmus + nächstes Datum), halbmonatlich auch mit Mengen aus dem Kalender (Abschnitt 18). Liste, Pausieren, „Bestellung bearbeiten“ (öffnet die offene Bestellung bzw. Rechnung des Zeitraums, gibt es keine, legt es die Bestellung zum nächsten Termin an): Verkauf → Symbol „Wiederkehrende Rechnungen“; Positionen ändern in der Verwaltung. Der Befehl `wiederkehrende_rechnungen` (täglich per Cron, Zeile `30 5 * * *` in `/etc/cron.d/wwok`) legt zum Termin eine **Bestellung** an (mit Bestellnummer, nicht vorläufig, nie automatisch verworfen; Lagerartikel reservieren ab dann Bestand). Daraus wird von Hand die Rechnung (Übernehmen, nie vor dem Stichtag datiert) oder der Lieferschein.
- **XRechnung:** Bei abgeschlossener Rechnung/Gutschrift Mehr → „XRechnung (XML) herunterladen“. Pflicht: Leitweg-ID am Kunden (Kunde bearbeiten → Steuer & Bank) sowie Telefon und E-Mail des Mandanten.
- **Dialoge:** Rückfragen und Fehlermeldungen erscheinen als eigenes Modal statt als Browser-Fenster (`data-frage` am Formular oder Knopf, `wwokDialog`, `wwokMeldung`). Beim Abschließen eines Belegs lassen sich im Modal per Häkchen „PDF erstellen“, „E-Mail senden“ und „Drucken“ wählen, auch zusammen: Das PDF öffnet sich auf der Ansichtsseite (bei „Drucken“ mit Druckdialog), die E-Mail folgt von dort aus.
- **Bestellung – Markierung „L“ und „R“:** Ist aus einer Bestellung ein Lieferschein entstanden, trägt sie in den Belegeslisten (Alle Belege, Kunde, Start, offene Posten) ein grünes „L“ mit Link zum Lieferschein (Tooltip: alle Lieferscheinnummern) und im Kopf „L · LS…“. Wurde sie direkt in eine Rechnung übergeben (Kopieren als Rechnung), steht dort ebenso ein grünes „R“ mit Link zur Rechnung und im Kopf „R · RE…“; eine Rechnung aus dem Lieferschein zählt nicht als „R“ der Bestellung. Ein Angebot entsteht beim Übergeben nur, wenn die Bestellung Preislistenartikel (P) enthält.
- **Angebot – Folgeartikel in die Gruppe:** Nach einem G-Artikel (oder einer freien Gruppe) ist beim Erfassen „zu Gruppe“ auf die zuletzt angelegte Gruppe vorbelegt; jede weitere Position landet dort, bis man sie für die aktuelle Position auf „–“ stellt (gilt nur für diese Position). Ein neuer G-Artikel wird selbst zur Gruppe und zur neuen Vorgabe; freie Textpositionen und Artikel mit anderem Steuersatz landen ohne Gruppe (mit Hinweis). Ein Rabatt an einer Gruppenposition wird nicht mehr still verworfen, sondern gemeldet.
- **PDF – Summenblock zusammen:** Summe netto, Umsatzsteuer und Gesamtbetrag (samt Versandkosten, Belegrabatt, Anzahlung/Restbetrag) stehen im Beleg-PDF immer zusammen auf einer Seite; passt der Block nicht mehr, wandert er komplett auf die nächste. Ebenso Netto/Steuer/Gesamt auf dem Werkstatt-Abholschein.
- **Wareneingang – Artikel im Formular anlegen:** Bei Preislistenartikeln ohne eigenen Artikel (Hinweiskasten und Scan-Rückfrage) öffnet „Im Formular anlegen …“ die Artikelmaske, vorausgefüllt mit Bezeichnung, Hersteller, Herstellernummer, EAN, Gewicht, Maßen und Import-EK des Lieferanten. Alles lässt sich vor dem Speichern ändern; erst „Speichern“ legt den A-Artikel an, verknüpft die Lieferantenzeilen, stellt offene Bestell- und Verkaufspositionen auf A um und übernimmt die offene Menge (beim Scan die gescannte) in den Wareneingang. Abbrechen ändert nichts; ist der Wareneingang inzwischen gebucht, wird nichts angelegt.
- **Alle Belege – Live-Filter:** Suchtext (nach kurzer Tipp-Pause), Belegart, Status und Zeitraum filtern sofort und kombiniert, ohne Klick auf „Filtern“; nur die Liste wird ausgetauscht, der Cursor bleibt im Suchfeld. Die Filter stehen in der Adresse (Neuladen, Zurück, Link teilen); ein halb eingetipptes Datum lässt den bisherigen Datumsfilter stehen. Ohne JavaScript bleibt der Knopf „Filtern“.
- **Versand am Beleg sichtbar:** Lieferschein, Rechnung und Nachlieferschein haben in der Aktionsleiste das Versand-Symbol (gefüllt, sobald ein Label existiert). Im Belegkopf steht je Sendung „Label vorhanden · Produkt · Nummer · gedruckt am …“ (Klick öffnet das Label; jedes Öffnen wird als gedruckt vermerkt). In der Kundenansicht und unter „Alle Belege“ zeigt ein grünes Lieferwagen-Symbol Belege mit Versandlabel.
- **Versand (DHL Paket):** Bei Lieferschein, Rechnung und Nachlieferschein über das Versand-Symbol: Sendungsnummer und Gewicht erfassen, internen Paketaufkleber (A6-PDF mit Empfänger und Barcode) drucken, Link zur Sendungsverfolgung, Versand-Mail an den Kunden (im Mailprotokoll vermerkt). Das amtliche DHL-Label kommt vorerst aus der DHL-Online-Frankierung; die direkte DHL-API-Anbindung (Label automatisch) braucht Geschäftskundenzugang (Zugangsdaten nur in der `.env`).
- **DHL-API (Label automatisch):** Auf der Versandseite „DHL-Label automatisch erzeugen“ (Parcel DE Shipping API v2): Label (PDF) und Sendungsnummer kommen direkt von DHL; Löschen der Versendung storniert die Sendung bei DHL. Zugangsdaten je Mandant nur in der `.env`: `DHL_API_KEY_<MANDANT>`, `DHL_BENUTZER_<MANDANT>`, `DHL_PASSWORT_<MANDANT>`, `DHL_ABRECHNUNG_<MANDANT>` (Kurzname in Großbuchstaben, z. B. `DHL_API_KEY_OKAY`); mit `DHL_TEST=1` läuft alles gegen die DHL-Testumgebung. Inland `V01PAK`, Ausland `V53WPAK`.
- **Letzter Preis des Kunden:** In Angebot, Kundenbestellung, Lieferschein und Rechnung steht je Position und bei der Artikelauswahl, was der Kunde zuletzt bezahlt hat: netto je Stück nach Positions- und Belegrabatt, aus der jüngsten abgeschlossenen Rechnung. Gutgeschriebene Mengen und vollständig gutgeschriebene Rechnungen zählen nicht.
- **Lieferantenvergleich:** Einkauf → Lieferantenvergleich, nach der Artikelauswahl in der Bestellung und je Entwurfsposition. Verglichen wird Ware + zusätzlich ausgelöster Versand (Versandkosten, versandkostenfrei ab), sortiert nach lieferbar, Mindestbestellwert erreicht, Gesamtkosten; ein offener Entwurf beim Lieferanten zählt mit. Gleiches Produkt heißt: verknüpft, gleiche EAN oder gleiche Herstellernummer bei passendem Hersteller. Entschieden wird von Hand („Hier bestellen“ bzw. „Hierhin verschieben“); fehlende Versandkosten werden als „?“ gezeigt und nie als „am günstigsten“ ausgewiesen. Gleicher Artikel zu anderem EK bleibt in der Bestellung eine eigene Zeile.
- **Paketgewicht und Produkt:** Das Gewicht wird aus den Artikelgewichten vorbelegt (Menge × Gewicht; Gruppenartikel mit eigenem Gewicht zählen statt ihrer Teile; Leistungen und ESD-Keys wiegen nichts). DHL Paket wird mit mindestens 1 kg angemeldet. DHL Kleinpaket (früher Warenpost, `V62KP`) höchstens 1 kg und nur Inland, Abrechnungsnummer EKP + 62 + 01 (oder `DHL_ABRECHNUNG_KLEINPAKET_<MANDANT>`); vorgeschlagen nur, wenn alle Artikel ein Gewicht haben.
- **Deutsche Post Brief (INTERNETMARKE):** Auf der Versandseite „Briefmarke kaufen & Etikett drucken“ – Standardbrief oder Großbrief, bezahlt aus der Portokasse, Etikett im Format DYMO 99014 (101 × 54 mm) mit Absender und Empfänger, nur Inland. Porto und Produktcodes unter Verwaltung → Briefprodukte (bei Portoerhöhung anpassen). Gekaufte Marken werden nie gelöscht, sondern über „Erstatten“ zurückgegeben; ein Kauf ist unter Sperre vorab eingetragen, eine verlorene Antwort erscheint als „Kauf unklar – Portokasse prüfen“ und sperrt weitere Käufe für den Beleg. Zugangsdaten nur in der `.env`: `DHL_API_SECRET_<MANDANT>` (zur selben App wie `DHL_API_KEY_<MANDANT>`), `DP_PORTOKASSE_BENUTZER_<MANDANT>`, `DP_PORTOKASSE_PASSWORT_<MANDANT>`, optional `DP_SEITENFORMAT_<MANDANT>`. In der Portokasse die Geschäftsanwendung freigeben. Prüfen ohne Kauf: `python manage.py internetmarke_pruefen OKAY`.
