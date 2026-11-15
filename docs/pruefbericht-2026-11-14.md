# wwok – Prüfbericht vom 14.11.2026

Gesamtprüfung nur durch Lesen des Codes (Stand `main` ea716a6 + Lieferant-Menü + Umstellung „nicht abgeschlossen“). Fünf Prüfläufe: Verkauf, Einkauf, Lager/Finanzen/Werkstatt/ESD/Shop, Sicherheit/Mandanten/Kern/Stammdaten, Abgleich mit README und Pflichtenheft. Nichts wurde geändert.

„selbst bestätigt“ = von mir zusätzlich am Code nachgelesen. Alle anderen Befunde stammen aus den Prüfläufen und sind dort am Code belegt, aber nicht nachgestellt.

## Stand der Bearbeitung (15.11.2026)

| Abschnitt | Stand |
|---|---|
| A | alle 12 Befunde behoben und live |
| B | B2 (Python 3.12, Django 5.2) und B8 (Preislisten-Ablage) erledigt; B1, B3, B4, B5, B6, B7 offen |
| C | 19 Fehler behoben; von den 14 Entscheidungen 12 umgesetzt. Offen: Mahngebühr als Forderung und DATEV-Konten PayPal/Karte/BU-Schlüssel (Steuerberater) |
| D | unbearbeitet |
| E | 1 bis 3 entschieden und umgesetzt (Bestellung bleibt löschbar, die Nummer bleibt verbraucht; jede Rechnung trägt das Abschlussdatum; Kundensperre auch bei Kopie und wiederkehrender Rechnung). 4 bis 8 offen |
| F | README nachgeführt. Kein Widerspruch zur freien Gutschrift gefunden: README 18 und Code sagen beide „nur aus einer Rechnung“ |
| G | Tests zu A und C ergänzt (Suite rund 1.560 Tests); eigene Testrunde für Werkstatt und ESD steht aus |

Der Text darunter ist der Befund vom 14.11.2026 und wurde nicht verändert.

## Was in Ordnung ist

- Mandantentrennung: Alle Views mit Beleg-ID filtern auf den Mandanten, keine Lücke gefunden. Jede View verlangt Anmeldung.
- Bestand ≥ 0 wird zentral mit Sperre geprüft. Lagerbewegungen, Zahlungen und Kassenbuch sind unveränderlich.
- Wareneingang kann nicht doppelt gebucht werden. Nummern werden unter Sperre vergeben.
- Geld wird überall in Decimal gerechnet. Kein `csrf_exempt`, kein rohes SQL, kein ungefiltertes HTML in Templates.
- Rund 100 von 125 Anforderungen aus README/Pflichtenheft sind umgesetzt. Kein TODO/FIXME im Code.

## A. Echte Fehler mit Geld- oder Bestandsfolgen (zuerst beheben)

| Nr | Bereich | Fehler | Folge | Fundstelle |
|---|---|---|---|---|
| A1 | Verkauf | Rechnung aus Lieferschein: Dort ergänzte oder erhöhte Lagerartikel werden nie abgebucht (selbst bestätigt: `bucht_bestand` bucht bei Rechnung mit Lieferschein nichts) | Bestand dauerhaft zu hoch | `apps/lager/services.py:14-28`, `apps/verkauf/views.py:454-656` |
| A2 | Einkauf | Lieferanten-Gutschrift als E-Rechnung (UBL CreditNote, CII 381) wird als offene Rechnung angelegt (selbst bestätigt) | Gutschrift würde per SEPA überwiesen | `apps/einkauf/erechnung.py:63-91` |
| A3 | Shop | Gutscheine/Rabatte der Shop-Bestellung werden verworfen, gebucht wird aber der rabattierte Zahlbetrag (selbst bestätigt) | Rechnung bleibt mit dem Gutscheinbetrag offen und läuft in die Mahnung | `apps/shop/services.py:139-163` |
| A4 | Einkauf | Preislisten-Import löst den Verweis bestellter P-Positionen, wenn der Lieferant den Artikel nicht mehr listet (selbst bestätigt) | Ware lässt sich nicht mehr gegen die Bestellung buchen | `apps/einkauf/preislisten.py:337-348`, `services.py:380, 501` |
| A5 | Einkauf | Teilgelieferte Bestellung lässt sich nie abschließen (kein „Rest nicht mehr lieferbar“) | Rest bleibt ewig offen, Bestellvorschlag rechnet zu wenig | `apps/einkauf/views.py:338-377`, `services.py:236` |
| A6 | Finanzen | Bankumsatz kann nach „Ignorieren → Wieder öffnen“ ein zweites Mal gebucht werden | doppelter Zahlungseingang | `apps/finanzen/views.py:347-355`, `bank.py:232` |
| A7 | Finanzen | Kunden-Skonto fehlt ganz (Felder vorhanden, nie genutzt) | Skontozahler landen im Mahnvorschlag | `apps/finanzen/services.py:88-96` |
| A8 | Finanzen | Lastschrift: Einzug erzeugt keinen Zahlungseingang, Rücklastschrift wird ignoriert | Rechnungen bleiben offen bzw. fallen aus dem Mahnwesen | `apps/finanzen/views.py:456-478`, `bank.py:188-191` |
| A9 | Verkauf | Sammelrechnung und wiederkehrende Rechnung ohne Sperre | bei gleichzeitigem Klick doppelte Rechnung | `apps/verkauf/models.py:1528-1560, 1805-1839` |
| A10 | Werkstatt | Teile nach Rechnung/Garantiebuchung ergänzbar; negative Menge erzeugt Lagerzugang | Teile nie abgebucht bzw. Bestand ohne Ware | `apps/werkstatt/views.py:111-133`, `services.py:98-100` |
| A11 | ESD | Manuelle Key-Zuordnung ohne Mengen- und Belegartgrenze | Keys ohne Verkauf verbraucht | `apps/esd/views.py:147-161` |
| A12 | Lager | Inventur rechnet gegen den Bestand beim Buchen, nicht gegen den angezeigten Soll | falscher Bestand, wenn während des Zählens verkauft wird | `apps/lager/views.py:318-328` |

## B. Betrieb und Sicherheit

| Nr | Punkt | Risiko | Fundstelle |
|---|---|---|---|
| B1 | Jeder Push auf `claude/**` geht nach grünen Tests ohne Freigabe live, das Repo ist öffentlich | ungeprüfter Code oder datenlöschende Migration in Produktion | `.github/workflows/test-deploy.yml`, `claude.yml` |
| B2 | Django 5.1 und Python 3.10 bekommen keine Sicherheitsupdates mehr | bekannte Lücken bleiben offen | `requirements.txt:1` |
| B3 | Rücksprung beim Deploy setzt nur den Code zurück, nicht die Datenbank | alter Code auf neuem Schema | `test-deploy.yml:104-118` |
| B4 | Kein Limit für Anmeldeversuche, keine 2FA (gefordert) | Passwort-Raten | `config/settings.py`, `config/urls.py` |
| B5 | Änderungsprotokoll erfasst `.update()`-Wege nicht (Belegabschluss, Summen) | GoBD-Nachweis lückenhaft | `apps/kern/signals.py:43-55` |
| B6 | Protokoll speichert IBAN und ESD-Keys im Klartext, unlöschbar | Anonymisierung nicht erfüllbar | `apps/kern/signals.py:12-23` |
| B7 | Abhängigkeiten nicht gepinnt, GitHub-Actions nicht auf Commit gepinnt, `show_full_output` im öffentlichen Repo | Lieferkette, Logs öffentlich | `requirements.txt`, Workflows |
| B8 | Preislisten-Temporärdateien unter `/medien/` ohne Anmeldung abrufbar, nie gelöscht | EK-Preise über bekannte URL lesbar | `deploy/PLESK.md:83` |

## C. Mittlere und kleinere Fehler

Einkauf
- Ungebuchter Wareneingang lässt sich nicht löschen; er blockiert Storno/Löschen der Bestellung (`urls.py`, `views.py:341, 377`).
- Bestellposition löschen ohne Prüfung auf offenen Wareneingang, letzte Position und Status (`views.py:354-360`).
- E-Rechnung prüft den Empfänger-Mandanten nicht; Lieferant wird notfalls über Namensanfang geraten (`views.py:682-684`).
- Bestellvorschlag: negative oder 0-Mengen möglich, Text führt zu Fehler 500; offene nicht abgeschlossene Bestellungen zählen nicht mit, zweiter Klick legt doppelt an (`views.py:454`, `services.py:236`).
- Preislistenimport nicht in einer Transaktion; Verknüpfung allein über Herstellernummer/EAN ohne Herstellerprüfung (`preislisten.py:387-572, 400-462`).
- Scan trifft bei zwei Positionen desselben Artikels immer die erste (`services.py:447`).
- Rundung nicht kaufmännisch (`models.py:94, 176, 241`).
- Bestellmail geht vor der Positionsprüfung raus (`views.py:417`).
- Serverprüfungen fehlen: Wareneingang zu stornierter Bestellung, negativer EK, gesperrter Lieferant (`views.py:262, 481`).
- RMA: beliebiger Status, Doppelklick legt zwei RMA an; gebuchte Wareneingänge im Admin änderbar (`views.py:841-854`, `admin.py:26`).

Verkauf
- Bestellung mit Folgebelegen ist löschbar, entgegen README 18; frühere Nummer kann danach erneut vergeben werden (`models.py:383-413, 1279`).
- Nur vorläufige Rechnungen bekommen beim Abschluss das Tagesdatum; Werkstatt-Rechnung behält das Anlagedatum (`models.py:842`).
- E-Rechnung nimmt den Käufer aus dem Kundenstamm statt aus der Rechnungsanschrift; PDF/XML werden nicht archiviert (`zugferd.py:51, 144`).
- Proforma-Storno für vorläufige Proforma nur im Template gesperrt (`finanzen/anzahlung.py:288`).
- Spalte B der Gutschrift umgeht die Sperre abgeschlossener Gutschriften (`views.py:715`).
- Positions-Views sperren den Beleg nicht (Timing, Vermutung) (`views.py:454, 622, 682`).
- Negative Einzelpreise erlaubt; Gruppenartikel in wiederkehrender Rechnung ohne Komponenten; offener Gutschriftentwurf blockiert neue Gutschrift.

Lager, Finanzen, Werkstatt, ESD, Shop
- Seriennummern verwaisen beim Löschen einer Position und bei der Umbuchung zwischen Mandanten (`verkauf/views.py:682`, `lager/services.py:205`).
- Kasse kann über Barrückzahlung/Storno ins Minus laufen; Buchungsdatum rückwirkend wählbar (`finanzen/services.py:291`).
- Zahl- und Stornostatus aus dem Shop werden nicht nachgezogen (`shop/services.py:115`).
- Ø-EK wird bei manueller Buchung und Umbuchung nicht fortgeschrieben (`lager/services.py:129, 205`).
- Werkstatt: „abgeholt“ ohne Rechnung möglich; ESD-Rückgabe doppelt möglich (zwei Tabs).
- Mahngebühr ist keine Forderung; DATEV: PayPal/Karte auf Bankkonto, 0 % ohne BU-Schlüssel (Steuerberater).
- G-Artikel aus dem Shop: Komponenten werden beim Import nicht aufgelöst, also nicht abgebucht (`shop/services.py:147`).

Stammdaten
- Gleichzeitiges Anlegen führt zu Fehler 500 statt neuer Nummer (`masken.py:255, 395`).
- EAN kann doppelt vergeben werden; keine Dublettenprüfung bei Lieferanten (`forms.py:342`).
- Kundensperre greift nur bei „Beleg neu“, nicht bei Kopieren und wiederkehrender Rechnung (`verkauf/views.py:230`).

## D. Offene Stellen laut README/Pflichtenheft

Nicht umgesetzt: Zwei-Faktor-Login, revisionssichere Archivierung der Belege, mehrere Belege gemeinsam senden/drucken, Versand GLS/DPD/UPS, eBay/Kaufland, Newsletter mit Double-Opt-in, Reverse Charge, Differenzbesteuerung, Textbausteine, Kundendaten anonymisieren, Versandfeld in Lieferantenbestellungen, Lieferanten-Gutschrift mit Lagerbuchung.

Teilweise: Rechte (nur Admin/Mitarbeiter, keine Zuordnung Benutzer → Mandant), ausgehende E-Rechnung (gebaut, nicht als PDF/A-3 geprüft), Auswertungen (ohne Marge, Excel, freien Zeitraum), Kalkulations-EK je Mandant (nur im Preis-Radar), Kontoauszug nur je Kunde, DATEV ohne Kostenstelle, Shop-Zugang je Ansprechpartner, Abgleich Rechnung ↔ Wareneingang nur über die Summe, keine Teilzahlung bei Lieferantenrechnungen.

Offene Klärungen: TSE beim Barverkauf, USt und DATEV-Konto für Anzahlungen (Steuerberater), tägliche Datenbanksicherung auf dem Server prüfen.

## E. Entscheidungen, die du treffen musst

1. Darf eine Bestellung mit Folgebelegen gelöscht werden? README 18 sagt nein, der Code erlaubt es.
2. Sollen alle Rechnungen beim Abschluss das Tagesdatum bekommen?
3. Heißt „Kunde gesperrt“: keine neuen Belege, auch nicht per Kopie oder Abo?
4. Darf ein Lieferschein reservierte Ware verbrauchen?
5. Sollen Mitarbeiter nur bestimmte Mandanten sehen?
6. Negative Einzelpreise im Beleg erlauben?
7. Welcher EK gilt für VK und Lagerwert: Import-EK oder Ø-EK je Mandant? README 18 widerspricht sich hier.
8. Soll der Deploy eine Freigabe bekommen oder weiter automatisch laufen?

## F. Doku veraltet

README 8.2 (Versand „nicht umgesetzt“), 8.3 („140 Tests“, tatsächlich rund 1.380), 15 und 16 (erledigte Punkte stehen als offen), 19.1 (vier Befehle fehlen), Cron-Takt `belege_verwerfen` widersprüchlich, Typ P „automatisch zu A“ gegen „nur nach Rückfrage“, Shop-Kategorien „nie anlegen“ gegen Code, freie Gutschrift widersprüchlich.

## G. Tests

Werkstatt hat nur 6 Tests, ESD 16. Für A1, A2, A3 (Test verdeckt den Fehler), A5, A6, A9 gibt es keine Tests.

## Vorschlag zur Reihenfolge

1. A1–A4 und A6 (Geld/Bestand, klar umrissen).
2. B1–B3 (Deploy-Freigabe, Django 5.2, Migrationsprüfung).
3. A5, ungebuchten Wareneingang löschen, A9–A12.
4. Skonto und Lastschrift (A7, A8) nach fachlicher Abstimmung.
5. Entscheidungen aus E, danach Doku nachziehen.
