# wwok – Web-Warenwirtschaft für OKAY Computer und ZONE Handel

Eigene, webbasierte Warenwirtschaft als Ersatz für **Mention**. Mandantenfähig für **OKAY Computer** und **ZONE Handel**, im Browser über HTTPS erreichbar und auf dem eigenen Strato V-Server betrieben.

Diese Datei fasst **alles** zusammen, was bisher besprochen und entschieden wurde: Vorgeschichte, alle Anforderungen, Entscheidungen, Technik, Server, GitHub, die bisherigen Einrichtungsschritte mit allen aufgetretenen Problemen, den aktuellen Stand und die offenen Punkte.

| Was | Wo |
| --- | --- |
| Repository | `akuvvet/wwok` (privat) |
| Lokaler Ordner | `D:\Desktop\github\wwok` |
| Server | Strato V-Server, Ubuntu 22.04, Plesk, Python 3.10.12 |
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
| **P** – Preisliste | Lieferantenprodukte aus importierten Preislisten, ohne eigene Artikelanlage verkaufbar | nein, wird beim Wareneingang **automatisch zu A** |
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
8. Startseite (fällige Wiedervorlagen, offene Aufträge, fällige Mahnungen)

### 4.5 Verkaufsabwicklung

**Einstieg**

- Kundensuche nach Name oder Nummer
- Platzhalter `*` zeigt alle Kunden, `Mül*` alle, die mit „Mül“ beginnen
- Button **„Neukunde anlegen“** (mit Dublettenprüfung)
- **Freie Belegsuche** ohne Kundenauswahl

**Ablauf nach der Kundenauswahl**

1. **Ansprechpartnerfenster** öffnet sich → Ansprechpartner wählen
2. **Belegfenster** mit den gespeicherten Belegen des Kunden
   - Filter: alle, Angebote, Aufträge, Lieferscheine, Nachlieferscheine, Rechnungen, Gutschriften, **offene Belege**
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

- Angebot → Auftrag/Kundenbestellung → Lieferschein → Rechnung, jeweils **per Klick** übernehmen
- Angebot in eine **Lieferantenbestellung** kopieren (P-Artikel werden automatisch dem Lieferanten zugeordnet)
- **Proforma-Rechnung** (ohne Buchung)
- **Teillieferung:** Geliefertes wird berechnet, der Rest bleibt als offene Kundenbestellung verknüpft
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
- **Bestellvorschlag:** fester Mindestbestand pro Artikel, sonst automatisch aus dem Verkaufsverlauf; per Klick Bestellung beim **günstigsten Lieferanten**

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
- **Reservierung** für offene Bestellungen, automatisch; Anzeige Gesamt / reserviert / verfügbar
- Nur der **verfügbare** Bestand geht an Shop und Marktplätze (Schutz vor Doppelverkauf)
- **Inventur:** Zählliste, gezählte Mengen eingeben, Differenzen automatisch buchen, pro Mandant
- **Lagerbewertung** über Durchschnitts-EK
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
- Startseite: fällige Wiedervorlagen, offene Aufträge, fällige Mahnungen

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
| Preise | letzter EK **pro Mandant**, Durchschnitts-EK, VK1–VK3 mit Aufschlag, Steuersatz |
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
| Sprache | Python **3.10+** | wird bereits genutzt, Server hat 3.10.12 |
| Framework | Django 5.1 | Benutzer, Rechte, Formulare, Migrationen und Verwaltungsoberfläche schon eingebaut |
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

Noch nicht umgesetzt: `versand` (DHL/Deutsche Post), eBay/Kaufland, `auswertungen`, Anmeldung mit 2FA (siehe Abschnitt 16).

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

Die Pflege läuft zusätzlich über die Django-Verwaltungsoberfläche unter **`/verwaltung/`**. Kunden und Lieferanten werden im Alltag in einem **Dialog mit Registerkarten** bearbeitet (Kunde: Grunddaten, Einstufung & Hinweis, Steuer & Bank, Ansprechpartner, Anschriften, Mandant; Lieferant: Grunddaten, Konditionen, Bank, Ansprechpartner, Mandant). Der Dialog hat feste Größe und Position, nur der Inhalt scrollt.

**Weitere Module (alle produktiv)**

| App | Inhalt |
| --- | --- |
| `verkauf` | Startseite mit Kundensuche und offenen Belegen; Kundenkarte mit Belegliste (Filter, Seiten); Angebot → Auftrag → Lieferschein → Rechnung per Klick; **Beleg kopieren als …** (beliebige Belegart, Angebot wird auf Wunsch abgeschlossen); Sammelrechnung aus mehreren Lieferscheinen; Gruppen (G) mit Unterpositionen; Teillieferung; Gutschrift/Storno; PDF mit Seriennummern; Mailversand je Mandant mit Protokoll |
| `lager` | Bestand je Mandant, unveränderliche Lagerbewegungen, Reservierung, Inventur, Seriennummern, Retoure; Bestand darf nie unter 0 fallen |
| `finanzen` | Offene Posten, Zahlungen und Stornos mit Grund, Kontoauszug, Mahnwesen mit PDF/Mail, Bankimport (CAMT.053/CSV), Kassenbuch (gesperrt), DATEV-Export |
| `einkauf` | Lieferantenbestellungen (Suche über Lieferanten- und eigene Artikel), Wareneingang mit Scan, Lieferantenrechnungen (auch ZUGFeRD/XRechnung), SEPA-Export (pain.001), RMA, Bestellvorschlag, **Preislisten-Import** (FTP/SFTP/URL, Zeitplan, Assistent), Lieferantenliste mit Dialog |
| `werkstatt` | Geräteannahme, Abholschein, Status, Teile und Arbeit, Abrechnung, Übergang in RMA |
| `esd` | Key-Import, automatische Vergabe beim Verkauf (nur Kategorie Software), Volumenkeys, Retoure |
| `shop` | Shopware 6 über die Admin-API: Bestellimport, Bestands- und Preisabgleich je Shop |

**Getestet:** Die Testsuite (Abschnitt 19) deckt Modelle, Belegfluss, Lager- und Bestandsregeln, Preislisten-Import, Zahlungen, ESD, Shop und die Dialoge für Kunden/Lieferanten ab (aktuell 140 Tests).

### 8.4 Sicherheit in den Einstellungen

- Im Echtbetrieb (`DJANGO_DEBUG=false`): HTTPS-Umleitung, sichere Cookies, HSTS (nur für die Subdomain, nicht für die Shops), kein Einbetten in fremde Seiten
- Passwörter mindestens **12 Zeichen**
- Sitzung endet nach **10 Stunden** oder beim Schließen des Browsers

### 8.5 Konfiguration über `.env`

Grundwerte stehen in `.env.example`. Zusätzlich liest das Programm Geheimnisse **nur aus der `.env` auf dem Server**, nie aus dem Repository und nie aus der Datenbank:

| Variable | Zweck |
| --- | --- |
| `SMTP_PASSWORD_<MANDANT>` | Passwort des Mail-Postfachs je Mandant (z. B. `SMTP_PASSWORD_OKAY`); Server, Benutzer und Absender stehen am Mandanten |
| `PREISLISTE_PW_<ID>` | Passwort für den Abruf einer Lieferanten-Preisliste (`<ID>` = Nummer des Importprofils, z. B. `PREISLISTE_PW_1`) |
| `SHOP_SECRET_<ID>` | Zugangsschlüssel der Shopware-API je Shop |

Beim Prüfen der `.env` nur die **Variablennamen** anzeigen (`cut -d= -f1`), nie die Werte.

## 9. Server und Subdomain

### 9.1 Ausgangslage

- **Strato V-Server** mit **Plesk**, Ubuntu 22.04, Python 3.10.12
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
python3 --version        # Ergebnis: Python 3.10.12 → passt
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
```

Beide Befehle laufen nur, wenn Shops bzw. Preislisten-Profile mit Zeitplan eingerichtet sind.

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
- [x] Einkauf: Bestellungen, Wareneingang, Lieferantenrechnungen, RMA, Bestellvorschlag, SEPA-Export, Preislisten-Import (Typ P als eigene Tabelle)
- [x] Lager: Bestand je Mandant, Bewegungen, Inventur, Seriennummern, Bestandsregel (nie unter 0)
- [x] Finanzen: Offene Posten, Zahlungen mit Storno, Mahnwesen, Bankimport, Kassenbuch, DATEV
- [x] Werkstatt, ESD-Keys, Shopware-Anbindung
- [ ] Anmeldung mit 2FA (bewusst verschoben)
- [x] Lastschrift-Einzug (pain.008)
- [ ] Ausgehende E-Rechnung (ZUGFeRD/XRechnung) – Eingang ist umgesetzt
- [x] Proforma-Rechnung, Nachlieferschein, wiederkehrende Rechnungen
- [x] Versand mit DHL-Paket-Label und Sendungsnummer
- [x] Rechte: Admin (is_staff/Superuser) und Mitarbeiter; Storno, Lastschrift aufheben und Preislisten-Einrichtung nur Admin
- [x] Artikelmaske mit Registerkarten (Lager → „+ Neuer Artikel“), neue Artikelfelder, Artikelansicht mit bestellt/letztem Wareneingang/Lieferanten
- [x] Alle Belege (kundenübergreifende Liste), Versandarten-Stammtabelle mit Standardkosten, Wiedervorlage mit Startseiten-Übersicht, Belegkopf (Bearbeiter, Bezugnahme, Liefertermin, Angebot gültig bis)
- [ ] Deutsche Post, eBay, Kaufland, Newsletter mit Double-Opt-in, USt-ID-Prüfung, Kategorien-Sync mit Shopware
- [ ] Auswertungen (Umsatz, Marge, Topseller) und Startseiten-Übersicht mit Wiedervorlagen und fälligen Mahnungen

**Einrichtung**

- [x] Code lokal unter `D:\Desktop\github\wwok`
- [x] Repo `akuvvet/wwok` angelegt, `main` = Stand auf dem Server
- [x] Subdomain `erp.okaycomputer.de` in Plesk, Datenbank `wwok` (MariaDB), HTTPS
- [x] Server: Pakete, Deploy-Key, `git clone`, venv, `.env`, `migrate`, `collectstatic`, Superuser
- [x] Gunicorn-Dienst `wwok` (Port 8010) und nginx-Weiterleitung, Rechte gesetzt
- [x] Zeitpläne in `/etc/cron.d/wwok` (Shop-Abgleich, Preislisten)
- [x] Mailversand je Mandant getestet
- [ ] Tägliches Backup der Datenbank prüfen (Plesk-Backup und externe Kopie)
- [ ] Claude-GitHub-App installieren (nur nötig für Push aus der Web-Sitzung von Claude)

## 16. Nächste Schritte

1. Entscheidung zur **Lastschrift** (pain.008, Rechnungshinweis mit Mandatsreferenz und Gläubiger-ID) und Gläubiger-ID für beide Mandanten eintragen
2. **Anmeldung mit 2FA** und Benutzerrechte
3. Fehlerhafte Rechnung RE2026-00003 korrigieren (Storno oder Bestandsnachbuchung)
4. **Ausgehende E-Rechnung** (ZUGFeRD/XRechnung) vor 2027
5. Startseite mit Wiedervorlagen, fälligen Mahnungen und **Auswertungen**
6. Proforma-Rechnung, Nachlieferschein, wiederkehrende Rechnungen
7. Versand (DHL, Deutsche Post), danach eBay, Kaufland, Newsletter, USt-ID-Prüfung
8. Backup und Wiederherstellung einmal real durchspielen

## 17. Regeln für das Repository

- Die `.env` mit Passwörtern kommt **nie** ins Repository.
- `main` = Stand, der auf dem Server läuft. Entwicklung in eigenen Branches.
- Jede Änderung an Modellen braucht eine Migration (`python manage.py makemigrations`), die mit eingecheckt wird.
- Echte Kunden- oder Belegdaten **nie** einchecken.
- Änderungen am Pflichtenheft im Claude-Dokument pflegen und `docs/pflichtenheft.md` neu exportieren.

## 18. Fachregeln im Programm (verbindlich)

Diese Regeln sind mit dem Nutzer abgestimmt und im Code umgesetzt. Änderungen nur nach Rücksprache.

| Thema | Regel |
| --- | --- |
| Artikel Typ A | eigene Tabelle `Artikel` mit Lagerbestand |
| Artikel Typ P | **eigene Tabelle `Preislistenartikel`**, getrennt von A; neben dem Artikel steht der Kurzname des Lieferanten (z. B. API, Kosatec, Ingram). Beim Wareneingang entsteht daraus ein Artikel A |
| Preisliste neu einlesen | Artikel, die in der Preisliste nicht mehr vorkommen, werden entfernt (mit Schutz: bricht ab, wenn mehr als 50 % verschwinden würden) |
| Lagerbestand | darf **nie unter 0** fallen |
| Lieferschein und Rechnung | nur mit ausreichendem Bestand abschließbar, **Druck/PDF ebenfalls gesperrt**, solange der Bestand nicht reicht |
| Rechnung aus Lieferschein | erst nach **abgeschlossenem** Lieferschein (der bucht das Lager ab) |
| Teillieferung | bei mehreren Positionen und teilweisem Bestand: Rückfrage; lieferbare Positionen werden als eigener Beleg abgeschlossen, der Rest bleibt unverändert als Entwurf. Versandkosten laufen mit der Teillieferung |
| Beleg ohne Position | kann nicht abgeschlossen, versendet oder weitergeführt werden |
| Löschen | Angebot, Auftrag und noch nicht nummerierte Rechnungs-/Gutschriftentwürfe dürfen gelöscht werden, sofern keine Folgebelege, Mails, Zahlungen oder Lagerbuchungen daran hängen; Lieferscheine und abgeschlossene Rechnungen nicht |
| Sammelrechnung | die Auswahl „Ausgewählte Lieferscheine in Rechnung umwandeln“ erscheint nur bei **mindestens zwei** offenen Lieferscheinen; das Lieferscheindatum steht bei der Position |
| ESD-Keys | nur für Artikel der Kategorie **Software** |
| Positionsnummern | lückenlos 1, 2, 3 …, nach Löschen automatisch neu vergeben; Gruppen-Unterpositionen tragen die Nummer ihrer Gruppe |
| EK (errechnet) | Wareneingang (Durchschnitts-EK) hat Vorrang, sonst günstigster Preislisten-EK; Gruppenartikel (G) = Summe der Komponenten. Grundlage für VK per Aufschlag |
| Zahlungen | Eingang und Ausgang sind unveränderlich; Korrektur nur per Stornobuchung mit Storno-Grund |
| Abgeschlossene Rechnung/Gutschrift | gesperrt, Korrektur nur per Gutschrift |
| Nach Abschließen, PDF oder E-Mail | die Anwendung kehrt zur Belegübersicht des Kunden zurück |
| Beleg kopieren | jeder Beleg lässt sich als beliebige Belegart (außer Gutschrift) kopieren; beim Angebot wird gefragt, ob es danach abgeschlossen werden soll |
| Belegrabatt | Prozent je Beleg, wirkt auf alle Positionen netto vor der Steuer (je Steuersatz auf Cent gerundet); Versand, Verpackung und Versicherung bleiben unberührt. Wird in Folgebelege, Gutschrift, PDF, ZUGFeRD und DATEV übernommen; Sammelrechnung nur bei gleichem Rabatt |
| Lieferantenrechnung | Weicht das Netto vom Wareneingang um mehr als 1 Cent ab, wird es gemeldet; Zahlung und SEPA-Export sind gesperrt, bis die Beträge korrigiert oder die Abweichung mit Begründung bestätigt ist (Verlauf mit Benutzer und Zeit; ändert sich die Differenz, muss neu bestätigt werden). Dazu ein Notizfeld |
| Menü | Verkauf, Einkauf, Artikel (früher „Lager“), Werkstatt, Finanzen |

## 19. Betrieb: Datenimporte, Zeitpläne, Geheimnisse, Tests

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

Offene Posten aus Mention werden **nicht** importiert.

### 19.2 Preislisten-Import

- Einrichtung über Einkauf → „Preislisten & EK-Import“ mit dem **Assistenten** (Datei hochladen oder Abruf per FTP/SFTP/URL, Format wird erkannt, Spalten werden zugeordnet, Vorschau vor dem Import).
- Für P-Artikel genügen: Bestellnummer, Herstellernummer (MSKU), Bezeichnung, Hersteller und EK; EAN, Bestand und Lieferzeit sind optional.
- Ein gesperrter Lauf (`laeuft_seit`) verhindert, dass Cron und manueller Start gleichzeitig laufen. Der Import ist für über 140.000 Zeilen ausgelegt (Lauf nach dem ersten Mal etwa 20 Sekunden).

### 19.3 Geheimnisse und Rechte

- Passwörter und Schlüssel stehen nur in `.env` auf dem Server (Abschnitt 8.5), Rechte `chmod 600`.
- Wurde ein Passwort versehentlich ausgegeben oder in einen Chat kopiert: **sofort ändern**.

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

Zu einer abgeschlossenen Rechnung: **Mehr → Kopieren als → Nachlieferschein**. Eigener Nummernkreis `NL<Jahr>-<Nr.>` (sofort vergeben), Zahlungsbedingung „Betrag dankend erhalten“ (wird bei Bedarf angelegt), keine Versandkosten, beim Abschließen wird gefragt, ob die Ware vom Lager abgebucht wird: **ja** bei Ersatz für defekte Ware (Bestand wird geprüft, nie unter 0), **nein** bei bloßer Nachlieferung (schon mit Rechnung/Lieferschein abgebucht). Bis zur Antwort ist kein Druck möglich. Nicht über „Neuer Beleg“ anlegbar.

### Proforma-Rechnung, wiederkehrende Rechnungen, XRechnung

- **Proforma-Rechnung:** Belegart `PF` mit eigenem Nummernkreis, entsteht wie jede Kopie (Mehr → Kopieren als …) oder über „Neuer Beleg“. Kein Lagerabgang, keine offenen Posten; das PDF trägt den Hinweis „keine Rechnung im Sinne des UStG“. Die echte Rechnung entsteht per „Kopieren als Rechnung“.
- **Wiederkehrende Rechnungen:** In einer abgeschlossenen Rechnung Mehr → „Wiederkehrend einrichten“ (Rhythmus + nächstes Datum). Liste, Pausieren, „Entwurf jetzt“: Verkauf → Symbol „Wiederkehrende Rechnungen“; Positionen ändern in der Verwaltung. Der Befehl `wiederkehrende_rechnungen` (täglich per Cron, Zeile `30 5 * * *` in `/etc/cron.d/wwok`) legt zum Termin einen **Entwurf** an, der von Hand geprüft und abgeschlossen wird.
- **XRechnung:** Bei abgeschlossener Rechnung/Gutschrift Mehr → „XRechnung (XML) herunterladen“. Pflicht: Leitweg-ID am Kunden (Kunde bearbeiten → Steuer & Bank) sowie Telefon und E-Mail des Mandanten.
- **Versand am Beleg sichtbar:** Lieferschein, Rechnung und Nachlieferschein haben in der Aktionsleiste das Versand-Symbol (gefüllt, sobald ein Label existiert). Im Belegkopf steht je Sendung „Label vorhanden · Produkt · Nummer · gedruckt am …“ (Klick öffnet das Label; jedes Öffnen wird als gedruckt vermerkt). In der Kundenansicht und unter „Alle Belege“ zeigt ein grünes Lieferwagen-Symbol Belege mit Versandlabel.
- **Versand (DHL Paket):** Bei Lieferschein, Rechnung und Nachlieferschein über das Versand-Symbol: Sendungsnummer und Gewicht erfassen, internen Paketaufkleber (A6-PDF mit Empfänger und Barcode) drucken, Link zur Sendungsverfolgung, Versand-Mail an den Kunden (im Mailprotokoll vermerkt). Das amtliche DHL-Label kommt vorerst aus der DHL-Online-Frankierung; die direkte DHL-API-Anbindung (Label automatisch) braucht Geschäftskundenzugang (Zugangsdaten nur in der `.env`).
- **DHL-API (Label automatisch):** Auf der Versandseite „DHL-Label automatisch erzeugen“ (Parcel DE Shipping API v2): Label (PDF) und Sendungsnummer kommen direkt von DHL; Löschen der Versendung storniert die Sendung bei DHL. Zugangsdaten je Mandant nur in der `.env`: `DHL_API_KEY_<MANDANT>`, `DHL_BENUTZER_<MANDANT>`, `DHL_PASSWORT_<MANDANT>`, `DHL_ABRECHNUNG_<MANDANT>` (Kurzname in Großbuchstaben, z. B. `DHL_API_KEY_OKAY`); mit `DHL_TEST=1` läuft alles gegen die DHL-Testumgebung. Inland `V01PAK`, Ausland `V53WPAK`.
- **Letzter Preis des Kunden:** In Angebot, Kundenbestellung, Lieferschein und Rechnung steht je Position und bei der Artikelauswahl, was der Kunde zuletzt bezahlt hat: netto je Stück nach Positions- und Belegrabatt, aus der jüngsten abgeschlossenen Rechnung. Gutgeschriebene Mengen und vollständig gutgeschriebene Rechnungen zählen nicht.
- **Lieferantenvergleich:** Einkauf → Lieferantenvergleich, nach der Artikelauswahl in der Bestellung und je Entwurfsposition. Verglichen wird Ware + zusätzlich ausgelöster Versand (Versandkosten, versandkostenfrei ab), sortiert nach lieferbar, Mindestbestellwert erreicht, Gesamtkosten; ein offener Entwurf beim Lieferanten zählt mit. Gleiches Produkt heißt: verknüpft, gleiche EAN oder gleiche Herstellernummer bei passendem Hersteller. Entschieden wird von Hand („Hier bestellen“ bzw. „Hierhin verschieben“); fehlende Versandkosten werden als „?“ gezeigt und nie als „am günstigsten“ ausgewiesen. Gleicher Artikel zu anderem EK bleibt in der Bestellung eine eigene Zeile.
- **Paketgewicht und Produkt:** Das Gewicht wird aus den Artikelgewichten vorbelegt (Menge × Gewicht; Gruppenartikel mit eigenem Gewicht zählen statt ihrer Teile; Leistungen und ESD-Keys wiegen nichts). DHL Paket wird mit mindestens 1 kg angemeldet. DHL Kleinpaket (früher Warenpost, `V62KP`) höchstens 1 kg und nur Inland, Abrechnungsnummer EKP + 62 + 01 (oder `DHL_ABRECHNUNG_KLEINPAKET_<MANDANT>`); vorgeschlagen nur, wenn alle Artikel ein Gewicht haben.
- **Deutsche Post Brief (INTERNETMARKE):** Auf der Versandseite „Briefmarke kaufen & Etikett drucken“ – Standardbrief oder Großbrief, bezahlt aus der Portokasse, Etikett im Format DYMO 99014 (101 × 54 mm) mit Absender und Empfänger, nur Inland. Porto und Produktcodes unter Verwaltung → Briefprodukte (bei Portoerhöhung anpassen). Gekaufte Marken werden nie gelöscht, sondern über „Erstatten“ zurückgegeben; ein Kauf ist unter Sperre vorab eingetragen, eine verlorene Antwort erscheint als „Kauf unklar – Portokasse prüfen“ und sperrt weitere Käufe für den Beleg. Zugangsdaten nur in der `.env`: `DHL_API_SECRET_<MANDANT>` (zur selben App wie `DHL_API_KEY_<MANDANT>`), `DP_PORTOKASSE_BENUTZER_<MANDANT>`, `DP_PORTOKASSE_PASSWORT_<MANDANT>`, optional `DP_SEITENFORMAT_<MANDANT>`. In der Portokasse die Geschäftsanwendung freigeben. Prüfen ohne Kauf: `python manage.py internetmarke_pruefen OKAY`.
