"""Live-Rechnen im Verkaufsbeleg (Wunsch des Nutzers 20.11.2026): Ausdrücke für die gemeinsame Funktion templates/rechnen.html.

Sie bilden Beleg.berechne() nach: Positionen je Steuersatz summiert, Belegrabatt je Satz auf Cent gerundet abgezogen,
Nebenkosten (Versand, Verpackung, Versicherung) auf den höchsten Satz der Positionen (ohne Positionen 19 %), Steuer je Satz
auf Cent gerundet. Eingaben aus dem Tab „Versand & Zahlung“: belegrabatt, versand, verpackung, versicherung."""
from decimal import Decimal

NEBENKOSTEN = "(versand + verpackung + versicherung)"


def summen_ausdruecke(beleg, positionen) -> dict:
    roh = {}
    for p in positionen:
        roh[p.steuer_prozent] = roh.get(p.steuer_prozent, Decimal("0")) + p.netto_gesamt
    saetze = sorted(roh) or [Decimal("19")]
    hoechster = max(saetze)
    konstanten, basis, rabatt, steuer = [], [], [], []
    for i, satz in enumerate(saetze):
        name = f"satz{i}_netto"
        konstanten.append((name, roh.get(satz, Decimal("0"))))
        rabatt_i = f"runde({name} * belegrabatt / 100)"
        basis_i = f"({name} - {rabatt_i})" + (f" + {NEBENKOSTEN}" if satz == hoechster else "")
        rabatt.append(rabatt_i)
        basis.append(f"({basis_i})")
        steuer.append(f"runde(({basis_i}) * {satz.normalize():f} / 100)")
    netto = "runde(" + " + ".join(basis) + ")"
    return {
        "konstanten": konstanten,
        "rabatt": " + ".join(rabatt),
        "netto": netto,
        "steuer": " + ".join(steuer),
        "brutto": f"{netto} + " + " + ".join(steuer),
    }
