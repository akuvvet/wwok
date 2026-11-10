"""
Vorläufige Belege: Was in der Belegmaske neu entsteht oder umgewandelt wird, ist erst mit „Speichern: abschließen & Nummer
vergeben“ endgültig. Bis dahin hält ein BelegVorgang den Ausgangszustand von Quelle und Beleg fest; „Verwerfen“ (Rückfrage beim
Verlassen, „Löschen“ am Beleg, Bereinigung nach 2 Stunden ohne Aktivität) stellt ihn wieder her, als hätte es den Vorgang nie gegeben.

- stand_sichern / vorgang_beginnen: Ausgangszustand festhalten (vor der ersten Änderung an der Quelle)
- beleg_verwerfen: alles zurücknehmen (neuer Beleg, Folgebeleg, Kopie, Umwandlung, Gutschrift, Sammelrechnung)
- nachholen: beim Abschluss das Vorgemerkte ausführen (P-Angebot nummerieren, Quelle abschließen)

Ein vorläufiger Beleg hat nie eine Nummer, ein Verwerfen erzeugt also keine Lücke im Nummernkreis.
"""
import logging
from decimal import Decimal

from django.core import serializers
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import Beleg, BelegUebernahme, BelegVorgang, Belegart, Nummernkreis, Position, Vorgangsweg

logger = logging.getLogger(__name__)

FOLGEARTEN = (Belegart.LIEFERSCHEIN, Belegart.RECHNUNG, Belegart.PROFORMA)
# Kopffelder, die in der Belegmaske (views.beleg_kopf) frei geändert werden können
KOPFFELDER = ("ansprechpartner", "zahlungsbedingung", "rechnungsanschrift", "versandanschrift", "versandart", "versandkosten", "verpackung",
              "versicherung", "referenz", "rabatt_prozent", "bezugnahme", "vortext", "schlusstext", "bemerkung")


def _benutzer():
    from apps.kern.middleware import aktueller_benutzer
    b = aktueller_benutzer()
    return b if b is not None and b.is_authenticated else None


def aktiv_melden(beleg) -> int:
    """Merkt die Aktivität am vorläufigen Beleg (Puls, Aufruf, Positionsänderung); ein einzelnes update, nur bei vorläufig."""
    return Beleg.objects.filter(pk=beleg.pk if hasattr(beleg, "pk") else beleg, vorlaeufig=True).update(zuletzt_aktiv=timezone.now())


# ---- Ausgangszustand ----------------------------------------------------------------------------------------------------
def stand_sichern(beleg, umwandlung=False) -> dict:
    """Ausgangszustand eines Belegs vor einem Vorgang: Kopf (Nebenkosten, Status), Positionen (serialisiert, mit Schlüsseln),
    Fingerabdruck der Mengen, Seriennummern und ESD-Vergaben je Position. umwandlung=True sichert zusätzlich den Kopf des Belegs
    selbst (Gültigkeit, Liefertermin, Abschluss, Datum, Bearbeiter), weil er umgeschrieben wird."""
    positionen = list(beleg.positionen.all())
    stand = {
        "quelle_kopf": {"versandkosten": str(beleg.versandkosten), "verpackung": str(beleg.verpackung),
                        "versicherung": str(beleg.versicherung), "abgeschlossen": beleg.abgeschlossen,
                        "automatisch_erledigt": beleg.automatisch_erledigt},
        "quelle_positionen": serializers.serialize("json", positionen),
        "quelle_fingerprint": [[p.pk, str(p.menge), p.geaendert_am.isoformat()] for p in positionen],
        "gruppen": {str(p.pk): p.gruppe_id for p in positionen if p.gruppe_id},
        "seriennummern": list(beleg.positionen.filter(seriennummern__isnull=False).values_list("pk", "seriennummern")),
        "esd": list(beleg.positionen.filter(esd_vergaben__isnull=False).values_list("pk", "esd_vergaben")),
        "beruehrt": [],
    }
    if umwandlung:
        stand["beleg_kopf"] = {
            "gueltig_bis": beleg.gueltig_bis.isoformat() if beleg.gueltig_bis else None,
            "liefertermin": beleg.liefertermin.isoformat() if beleg.liefertermin else None,
            "abgeschlossen": beleg.abgeschlossen, "automatisch_erledigt": beleg.automatisch_erledigt,
            "belegdatum": beleg.belegdatum.isoformat(), "bearbeiter_id": beleg.bearbeiter_id}
        stand["beleg_positionen"] = stand["quelle_positionen"]
        # alle Felder, die die Belegmaske im vorläufigen Beleg ändern kann (beleg_kopf): Verwerfen nimmt sie zurück
        stand["kopf_felder"] = serializers.serialize("json", [beleg], fields=KOPFFELDER)
    return stand


def vorgang_beginnen(beleg, weg, quelle=None, stand=None, angebot=None) -> BelegVorgang:
    """Legt den Vorgang zum (soeben als vorläufig angelegten oder umgewandelten) Beleg an."""
    return BelegVorgang.objects.create(beleg=beleg, weg=weg, quelle=quelle, angebot=angebot, stand=stand or {}, benutzer=_benutzer())


def eigenen_vorgang_sichern(beleg) -> dict | None:
    """Ein nicht gespeichertes Angebot hat schon einen eigenen Vorgang (neu angelegt). Wird es umgewandelt, braucht der Beleg den
    Vorgang der Umwandlung (OneToOne): der bisherige wird gesichert und entfernt; beim Verwerfen holt _umwandlung_zurueck ihn zurück."""
    alt = BelegVorgang.objects.filter(beleg=beleg).first()
    if alt is None:
        return None
    gesichert = {"weg": alt.weg, "stand": alt.stand, "benutzer_id": alt.benutzer_id, "angelegt_am": alt.angelegt_am.isoformat(),
                 "quelle_id": alt.quelle_id, "angebot_id": alt.angebot_id,
                 "quelle_abgeschlossen_durch_vorgang": alt.quelle_abgeschlossen_durch_vorgang}
    alt.delete()
    return gesichert


def _eigenen_vorgang_zurueck(beleg, gesichert):
    from datetime import datetime
    from django.contrib.auth import get_user_model
    quelle_id = gesichert.get("quelle_id") if Beleg.objects.filter(pk=gesichert.get("quelle_id")).exists() else None
    angebot_id = gesichert.get("angebot_id") if Beleg.objects.filter(pk=gesichert.get("angebot_id")).exists() else None
    benutzer_id = gesichert.get("benutzer_id") if get_user_model().objects.filter(pk=gesichert.get("benutzer_id")).exists() else None
    neu = BelegVorgang.objects.create(
        beleg=beleg, weg=gesichert["weg"], stand=gesichert.get("stand") or {}, benutzer_id=benutzer_id, quelle_id=quelle_id, angebot_id=angebot_id,
        quelle_abgeschlossen_durch_vorgang=gesichert.get("quelle_abgeschlossen_durch_vorgang", False))
    BelegVorgang.objects.filter(pk=neu.pk).update(angelegt_am=datetime.fromisoformat(gesichert["angelegt_am"]))  # auto_now_add


def abschluss_vormerken(ziel, modus) -> bool:
    """Das Abschließen der Quelle (Bestellung erledigt bzw. Urbeleg abschließen) wird erst beim Abschluss des vorläufigen Ziels
    ausgeführt. modus: "erledigt" (Bestellung ohne offene Reste), "urbeleg" oder "angebot" (nicht gespeichertes Angebot: bekommt
    seine Nummer mit dem Abschluss des Ziels, siehe angebot_vorab). True, wenn vorgemerkt wurde."""
    vorgang = BelegVorgang.objects.filter(beleg=ziel).first() if ziel.vorlaeufig else None
    if vorgang is None:
        return False
    vorgang.stand = {**vorgang.stand, "abschluss_modus": modus}
    vorgang.quelle_abgeschlossen_durch_vorgang = True
    vorgang.save()
    return True


def beruehrt(ziel, position, menge=None):
    """Merkt sich, dass der Vorgang eine Position der Quelle verändert hat (auch wenn sie später gelöscht wird). `menge` ist die
    Menge, die der Vorgang netto aus dieser Position ins Ziel gebracht hat (negativ beim Zurückbuchen); die Summe dient beim
    Verwerfen als Sollzustand (siehe _fingerprint_passt)."""
    if position is None or not ziel.vorlaeufig:
        return
    vorgang = BelegVorgang.objects.filter(beleg=ziel).first()
    if vorgang is None or "beruehrt" not in vorgang.stand:
        return
    geaendert = False
    if position.pk not in vorgang.stand["beruehrt"]:
        vorgang.stand["beruehrt"].append(position.pk)
        geaendert = True
    if menge is not None:
        netto = vorgang.stand.setdefault("netto", {})
        netto[str(position.pk)] = str(Decimal(netto.get(str(position.pk), "0")) + Decimal(menge))
        geaendert = True
    if geaendert:
        BelegVorgang.objects.filter(pk=vorgang.pk).update(stand=vorgang.stand)


# ---- Abschluss: Vorgemerktes ausführen ---------------------------------------------------------------------------------
def angebot_vorab(ziel):
    """Beim Abschluss des Ziels, vor dessen Nummer (in derselben Transaktion): Das nicht gespeicherte Angebot, aus dem das Ziel
    übernommen oder kopiert wurde (Modus "angebot"), wird abgeschlossen und nummeriert. Hat der Benutzer es inzwischen selbst
    gespeichert, geschieht nichts doppelt. Bei einer Umwandlung (derselbe Datensatz) bekommt das Angebot seine Nummer im Verlauf."""
    v = BelegVorgang.objects.filter(beleg=ziel).first()
    if v is None or v.stand.get("abschluss_modus") != "angebot":
        return
    if v.weg == Vorgangsweg.UMWANDLUNG:
        u = ziel.umwandlungen.order_by("-pk").first()
        if u is not None and u.von_art == Belegart.ANGEBOT and not u.von_nummer:
            u.von_nummer = Nummernkreis.naechste(ziel.mandant, Belegart.ANGEBOT, u.von_belegdatum.year)
            u.save(update_fields=["von_nummer"])
        return
    if v.quelle_id:
        quelle = Beleg.objects.select_for_update().filter(pk=v.quelle_id).first()
        if quelle is not None and quelle.art == Belegart.ANGEBOT and quelle.vorlaeufig and not quelle.abgeschlossen:
            try:
                quelle.abschliessen()
            except ValueError as fehler:
                raise ValueError("Das Angebot, aus dem dieser Beleg entstanden ist, lässt sich nicht abschließen "
                                 f"({str(fehler).rstrip('.')}) – bitte dort ergänzen oder diesen Beleg verwerfen.") from fehler


def nachholen(beleg):
    """Beim Abschluss des vorläufigen Belegs (in derselben Transaktion): P-Angebot nummerieren, vorgemerkten Abschluss der
    Quelle ausführen, dann den Vorgang löschen. Ab jetzt gilt alles endgültig."""
    vorgang = BelegVorgang.objects.filter(beleg=beleg).select_related("quelle", "angebot").first()
    if vorgang is None:
        return
    angebot = vorgang.angebot
    if angebot is not None and angebot.vorlaeufig:
        nummer = Nummernkreis.naechste(angebot.mandant, angebot.art, angebot.belegdatum.year)
        Beleg.objects.filter(pk=angebot.pk).update(nummer=nummer, vorlaeufig=False, zuletzt_aktiv=None)
    if vorgang.quelle_abgeschlossen_durch_vorgang and vorgang.quelle_id:
        quelle = Beleg.objects.select_for_update().get(pk=vorgang.quelle_id)
        modus = vorgang.stand.get("abschluss_modus")
        if not quelle.abgeschlossen:
            if modus == "urbeleg":
                quelle.abschliessen()
            elif modus == "erledigt":
                if not quelle.positionen.exists():
                    from .uebernahme import erledigt_wenn_leer
                    erledigt_wenn_leer(quelle)
                elif not quelle.offene_mengen():  # Proforma-Fälle ausgenommen: dort wird nichts vorgemerkt
                    quelle.abschliessen()
                    Beleg.objects.filter(pk=quelle.pk).update(automatisch_erledigt=True)
    BelegVorgang.objects.filter(beleg=beleg).delete()


# ---- Verwerfen ----------------------------------------------------------------------------------------------------------
def verwerfhindernis(beleg, vorgang=None):
    """Grund, warum ein vorläufiger Beleg nicht verworfen werden darf (None = in Ordnung). Absicherung für Altfälle, an denen
    doch etwas hängt (Mail, Zahlung, Lagerbuchung, Versand, ESD-Key, Lieferantenbestellung, Folgebeleg)."""
    from apps.einkauf.models import Bestellung
    from apps.shop.models import ShopBestellung
    from apps.werkstatt.models import Reparatur
    umwandlung = vorgang is not None and vorgang.weg == Vorgangsweg.UMWANDLUNG
    # Bei einer Umwandlung ist der Beleg derselbe Datensatz wie der Ausgangsbeleg: Mails, die vor der Umwandlung verschickt wurden
    # (gemailtes Angebot), bleiben beim Zurückwandeln erhalten und sind kein Hindernis.
    mails = beleg.mails.filter(zeitpunkt__gte=vorgang.angelegt_am) if umwandlung else beleg.mails
    if mails.exists():
        return "Der Beleg wurde per E-Mail versendet; der Versandnachweis muss erhalten bleiben."
    if beleg.zahlungen.exists():
        return "Zum Beleg gibt es Zahlungen – bitte zuerst stornieren."
    if beleg.anzahlung_verrechnungen.exists():
        return "Zahlungen dieses Belegs sind mit einer Rechnung verrechnet – er muss erhalten bleiben."
    if beleg.lagerbewegungen.exists():
        return "Zum Beleg gibt es Lagerbuchungen."
    if beleg.versendungen.exists():
        return "Zum Beleg gibt es einen Versand (Label oder Briefmarke)."
    if beleg.esd_vergaben.exists():
        return "Zum Beleg wurden Lizenzschlüssel vergeben."
    if not umwandlung and Bestellung.objects.filter(aus_beleg=beleg).exists():  # bei der Umwandlung: schon vorher am Ausgangsbeleg
        return "Aus dem Beleg wurde eine Lieferantenbestellung angelegt."
    if beleg.art == Belegart.ANGEBOT:
        offen = BelegVorgang.objects.filter(quelle_id=beleg.pk, quelle_abgeschlossen_durch_vorgang=True).exclude(beleg_id=beleg.pk) \
            .select_related("beleg").first()
        if offen is not None:
            return f"Aus dem Angebot ist der nicht gespeicherte Beleg {offen.beleg} entstanden: erst diesen speichern oder verwerfen."
    eigenes_angebot = vorgang.angebot_id if vorgang is not None else None
    if beleg.folgebelege.exclude(pk=eigenes_angebot).exists() or beleg.rechnungen.exists():
        return "Aus dem Beleg ist schon ein Folgebeleg entstanden."
    # Shop, Abo und Werkstatt: ein Beleg, der selbst dazugehört, wird nie verworfen. Ein vorläufiger Folgebeleg oder eine
    # Umwandlung aus einer Abo-/Shop-Bestellung dagegen schon: das Zurücknehmen stellt die Bestellung wieder her.
    aus_vorgang = vorgang is not None and vorgang.weg in (Vorgangsweg.UMWANDLUNG, Vorgangsweg.FOLGE, Vorgangsweg.KOPIE, Vorgangsweg.SAMMEL)
    if not aus_vorgang and (ShopBestellung.objects.filter(beleg=beleg).exists() or beleg.abo_id
                            or Reparatur.objects.filter(rechnung=beleg).exists()):
        return "Der Beleg gehört zu einer Shop-Bestellung, einem Abo oder einer Reparatur und wird nie automatisch verworfen."
    return None


def _loeschen(beleg):
    """Löscht den Beleg samt Positionen, ohne die Rückbuchung von Beleg.delete (die Mengen sind dann schon zurück)."""
    super(Beleg, beleg).delete()


def _seriennummern_pruefen(beleg_ids):
    from apps.lager.models import Seriennummer
    return list(Seriennummer.objects.filter(position__beleg_id__in=[i for i in beleg_ids if i]))


def _seriennummern_zuruecksetzen(sns, snapshot, ziel=None):
    """Seriennummern, die der Vorgang bewegt hat: Was im Ausgangszustand an einer Position der Quelle hing, geht dorthin zurück
    (verkauft); alles andere ist wieder im Lager. ziel: das Ziel, das gleich gelöscht wird. Positionen darin sind kein Rückweg
    (sie verschwinden mit dem Ziel): Eine Nummer, die der robuste Weg schon an eine Position der Bestellung zurückgehängt hat,
    bleibt dort, statt verwaist (verkauft, ohne Position) zu enden."""
    from apps.lager.models import Seriennummer
    alt = {sn: position for position, sn in snapshot}

    def im_ziel(position_id):
        return ziel is not None and position_id is not None and Position.objects.filter(pk=position_id, beleg=ziel).exists()
    for sn in sns:
        if sn.pk in alt and Position.objects.filter(pk=alt[sn.pk]).exists() and not im_ziel(alt[sn.pk]):
            Seriennummer.objects.filter(pk=sn.pk).update(position_id=alt[sn.pk], status=Seriennummer.VERKAUFT)
            continue
        aktuell = Seriennummer.objects.filter(pk=sn.pk).values_list("position_id", flat=True).first()
        if ziel is not None and aktuell is not None and not im_ziel(aktuell) and sn.pk in alt:
            continue  # schon an der Bestellposition (beleg_zurueckbuchen)
        Seriennummer.objects.filter(pk=sn.pk).update(position=None, status=Seriennummer.IM_LAGER)
    gemerkt = {sn.pk for sn in sns}
    for position, sn in snapshot:  # durch das Löschen einer Position von ihr gelöste Nummern
        if sn not in gemerkt and not im_ziel(position):
            Seriennummer.objects.filter(pk=sn, position__isnull=True).update(position_id=position, status=Seriennummer.VERKAUFT)


def _positionen_wiederherstellen(positionen_json, loeschen_aus):
    """Holt die Positionen des gesicherten Stands zurück, mit denselben Schlüsseln. Positionen der Belege in `loeschen_aus`,
    die nicht im Stand stehen, werden entfernt. Bezüge, die inzwischen fehlen, werden gelöst; ein Preislistenartikel, der
    inzwischen eingetroffen ist, bleibt Typ A (Wareneingang)."""
    from apps.stammdaten.models import Preislistenartikel
    alt = sorted(serializers.deserialize("json", positionen_json, ignorenonexistent=True),
                 key=lambda eintrag: eintrag.object.gruppe_id is not None)
    pks = [e.object.pk for e in alt]
    Position.objects.filter(pk__in=pks).update(gruppe=None)  # sonst nähme das Löschen einer Gruppe ihre alten Unterpositionen mit
    for beleg in loeschen_aus:
        Position.objects.filter(beleg=beleg).exclude(pk__in=pks).delete()
    for eintrag in alt:
        for feld in Position._meta.concrete_fields:
            wert = getattr(eintrag.object, feld.attname)
            if feld.is_relation and wert and not feld.related_model.objects.filter(pk=wert).exists():
                setattr(eintrag.object, feld.attname, None)
        p = eintrag.object
        if p.preisliste_artikel_id and not p.artikel_id:
            artikel_id = Preislistenartikel.objects.filter(pk=p.preisliste_artikel_id).values_list("artikel_id", flat=True).first()
            if artikel_id:
                p.artikel_id, p.typ, p.preisliste_artikel_id = artikel_id, "A", None
        eintrag.save()


def _esd_zurueck(paare):
    from apps.esd.models import ESDVergabe
    for position, vergabe in paare:
        ESDVergabe.objects.filter(pk=vergabe, position__isnull=True).update(position_id=position)


def _verlauf_entfernen(beleg):
    """Die Verlaufseinträge (BelegUebernahme) zum verworfenen Beleg verschwinden, als hätte es die Übernahme nie gegeben; das
    Änderungsprotokoll hält das Verwerfen fest. Einträge, deren Position inzwischen in einem anderen Beleg liegt (Teillieferung),
    bleiben. Bewusst nur hier: der Verlauf ist sonst unveränderlich (BelegUebernahme.delete), ein QuerySet-delete umgeht das."""
    BelegUebernahme.objects.filter(ziel=beleg).filter(Q(zielposition__isnull=True) | Q(zielposition__beleg=beleg)).delete()


def positionen_unveraendert(beleg, positionen_json) -> bool:
    """Hat der Beleg noch genau die Positionen des gesicherten Stands (Artikel, Menge, Preis, Rabatt, Steuer, Texte, Gruppen)?"""
    felder = ("artikel_id", "preisliste_artikel_id", "typ", "bezeichnung", "zusatztext", "menge", "einzelpreis", "rabatt_prozent",
              "steuer_prozent", "gruppe_id")
    alt = {e.object.pk: tuple(getattr(e.object, f) for f in felder)
           for e in serializers.deserialize("json", positionen_json, ignorenonexistent=True)}
    aktuell = {p.pk: tuple(getattr(p, f) for f in felder) for p in Position.objects.filter(beleg=beleg)}
    return alt == aktuell


def _fingerprint_passt(ziel, quelle, stand, angebot_id=None) -> bool:
    """Weg A ist nur sicher, wenn die Bestellung seit dem Vorgang nicht anders geändert wurde: alle Positionen, die der Vorgang
    nicht selbst verändert hat, haben noch Menge und Änderungszeit des Ausgangszustands, und es sind keine fremden dazugekommen.
    Auch die vom Vorgang berührten Positionen werden geprüft: Sie liegen noch in der Bestellung, im Ziel oder im P-Angebot (nie in
    einem dritten Beleg, z. B. einem zweiten Lieferschein), und Rest in der Bestellung plus netto im Ziel ergibt die Menge des
    Ausgangszustands (sonst hat jemand anderes dazwischen Mengen übernommen oder geändert)."""
    if not stand.get("quelle_fingerprint") and not stand.get("quelle_positionen"):
        return False
    beruehrt = set(stand.get("beruehrt", []))
    beruehrt |= {e.quellposition_id for e in BelegUebernahme.objects.filter(ziel=ziel) if e.quellposition_id}
    beruehrt |= set(Position.objects.filter(beleg=ziel, quellposition__isnull=False).values_list("quellposition_id", flat=True))
    beruehrt |= {int(pk) for pk, gruppe in stand.get("gruppen", {}).items() if gruppe in beruehrt}
    beruehrt |= set(Position.objects.filter(gruppe_id__in=beruehrt).values_list("pk", flat=True))
    aktuell = {p.pk: (str(p.menge), p.geaendert_am.isoformat()) for p in Position.objects.filter(beleg=quelle)}
    bekannt = set()
    erlaubt = {quelle.pk, ziel.pk, angebot_id}
    wo = dict(Position.objects.filter(pk__in=beruehrt).values_list("pk", "beleg_id"))
    netto = stand.get("netto", {})
    for pk, menge, am in stand["quelle_fingerprint"]:
        bekannt.add(pk)
        if pk in beruehrt:
            if pk in wo and wo[pk] not in erlaubt:
                return False  # inzwischen in einem dritten Beleg: dort nicht antasten
            if str(pk) in netto:
                rest = aktuell[pk][0] if pk in aktuell else "0"
                if Decimal(rest) + Decimal(netto[str(pk)]) != Decimal(menge):
                    return False
            continue
        if aktuell.get(pk) != (menge, am):
            return False
    return set(aktuell) - bekannt <= beruehrt


def _kopf_zurueck(quelle, kopf, erzwingen=False):
    """Versandkosten, Verpackung und Versicherung der Bestellung: zurück auf den Ausgangszustand, aber nur, wenn sie seither
    unverändert oder durch den Vorgang geleert sind (nichts überschreiben, was jemand danach eingetragen hat)."""
    jetzt = (quelle.versandkosten, quelle.verpackung, quelle.versicherung)
    vorher = tuple(Decimal(kopf[f]) for f in ("versandkosten", "verpackung", "versicherung"))
    if jetzt != vorher and (erzwingen or not any(jetzt)):
        quelle.versandkosten, quelle.verpackung, quelle.versicherung = vorher
        quelle.save(update_fields=["versandkosten", "verpackung", "versicherung"])


def _angebot_entfernen(angebot, quelle, stand, meldungen=None):
    """P-Angebot des Vorgangs: die Positionen des Ausgangszustands zurück an die Bestellung (Weg B; bei Weg A schon geschehen),
    Angebot löschen. Ein Angebot, das inzwischen eine Nummer hat oder abgeschlossen ist, wird nie angetastet (kein Loch im
    Nummernkreis, kein Verlust eines abgeschlossenen Belegs); hat der Benutzer eigene Positionen ergänzt, bleibt es ebenfalls
    bestehen und wird mit Nummer gespeichert – nur die P-Positionen der Bestellung gehen zurück."""
    if angebot is None:
        return
    angebot = Beleg.objects.filter(pk=angebot.pk).first()
    if angebot is None:
        return
    if angebot.nummer or angebot.abgeschlossen or not angebot.vorlaeufig:
        if meldungen is not None:
            meldungen.append(f"Das Angebot {angebot.nummer or ''} ist schon gespeichert und bleibt mit den Preislistenartikeln (P) bestehen.")
        return
    pos = {pk: pos for pk, pos in _pos_aus_stand(stand)}
    for p in Position.objects.filter(beleg=angebot, pk__in=pos):
        p.beleg = quelle
        p.pos = pos.get(p.pk, 0) or p.pos
        p.save()
    if angebot.folgebelege.exists():
        raise ValueError("Am P-Angebot hängen Folgebelege – das Verwerfen ist nicht möglich.")
    if angebot.positionen.exists():  # vom Benutzer ergänzte Positionen: das Angebot bleibt, mit Nummer gespeichert
        nummer = Nummernkreis.naechste(angebot.mandant, angebot.art, angebot.belegdatum.year)
        Beleg.objects.filter(pk=angebot.pk).update(nummer=nummer, vorlaeufig=False, zuletzt_aktiv=None)
        if meldungen is not None:
            meldungen.append(f"Das Angebot {nummer} enthält eigene Positionen und bleibt gespeichert; die Preislistenartikel (P) sind wieder in der Bestellung.")
        return
    _loeschen(angebot)


def _pos_aus_stand(stand):
    for eintrag in serializers.deserialize("json", stand.get("quelle_positionen", "[]"), ignorenonexistent=True):
        yield eintrag.object.pk, eintrag.object.pos


def _ziel_seriennummern_loesen(ziel):
    """Seriennummern an Positionen des Ziels (in der vorläufigen Phase zugeordnet) sind wieder im Lager."""
    from apps.lager.models import Seriennummer
    Seriennummer.objects.filter(position__beleg=ziel).update(position=None, status=Seriennummer.IM_LAGER)


@transaction.atomic
def beleg_verwerfen(beleg, benutzer=None, grund="") -> dict | None:
    """Nimmt einen vorläufigen Beleg samt allem zurück, was sein Vorgang an der Quelle verändert hat.
    Rückgabe: {"weiter": URL, "meldungen": [Text, …]}, oder None, wenn nichts zu verwerfen ist (nicht vorläufig, schon
    abgeschlossen oder schon verworfen). ValueError, wenn ein Hindernis besteht; dann ist nichts geändert."""
    vorab = BelegVorgang.objects.filter(beleg_id=beleg.pk).values_list("quelle_id", flat=True).first()
    if vorab:
        Beleg.objects.select_for_update().filter(pk=vorab).first()  # Reihenfolge wie beim Anlegen: erst die Quelle, dann das Ziel
    b = Beleg.objects.select_for_update().filter(pk=beleg.pk).first()
    if b is None:
        return None
    vorgang = BelegVorgang.objects.filter(beleg=b).select_related("quelle", "angebot").first()
    if not b.vorlaeufig or b.abgeschlossen or vorgang is None:
        return None
    hindernis = verwerfhindernis(b, vorgang)
    if hindernis:
        raise ValueError(hindernis)
    meldungen = []
    kunde_url = reverse("verkauf:kunde", args=[b.kunde_id])
    weiter = kunde_url
    wiedervorlagen = b.wiedervorlagen.count()
    if wiedervorlagen:
        meldungen.append(f"{wiedervorlagen} Wiedervorlage(n) zu diesem Beleg bleiben ohne Beleg bestehen.")
    stand = vorgang.stand or {}
    quelle = vorgang.quelle
    if vorgang.weg in (Vorgangsweg.NEU, Vorgangsweg.GUTSCHRIFT, Vorgangsweg.SAMMEL):
        _ziel_seriennummern_loesen(b)
        _loeschen(b)
    elif vorgang.weg == Vorgangsweg.UMWANDLUNG:
        weiter = _umwandlung_zurueck(b, vorgang)
    else:  # FOLGE, KOPIE
        if quelle is not None:
            weiter = reverse("verkauf:beleg", args=[quelle.pk])
        if quelle is not None and quelle.art == Belegart.AUFTRAG and b.art in FOLGEARTEN and b.vorgaenger_id == quelle.pk:
            if stand.get("quelle_positionen") and _fingerprint_passt(b, quelle, stand, vorgang.angebot_id):
                _weg_a(b, vorgang, quelle, stand, meldungen)
            else:
                _weg_b(b, vorgang, quelle, stand, meldungen)
                meldungen.append(f"Die Bestellung {quelle.nummer or ''} wurde zwischendurch geändert; die Mengen wurden zurückgebucht.")
        else:
            _ziel_seriennummern_loesen(b)
            if quelle is not None:
                _angebot_entfernen(vorgang.angebot, quelle, stand, meldungen)
            if stand.get("abschluss_modus") == "angebot":  # aus einem nicht gespeicherten Angebot: als hätte es die Übernahme nie gegeben
                _verlauf_entfernen(b)
            _loeschen(b)
            if stand.get("abschluss_modus") == "angebot" and quelle is not None:  # das Angebot bleibt unverändert, neue 2-Stunden-Frist
                Beleg.objects.filter(pk=quelle.pk, vorlaeufig=True, abgeschlossen=False).update(zuletzt_aktiv=timezone.now())
    logger.info("Beleg %s verworfen (%s, %s)", beleg.pk, vorgang.get_weg_display(), grund or "von Hand")
    return {"weiter": weiter, "meldungen": meldungen}


def _weg_a(ziel, vorgang, quelle, stand, meldungen=None):
    """Exakt zurück: die Bestellung bekommt ihre Positionen mit denselben Schlüsseln, Mengen, Positionsnummern und Gruppen aus
    dem gesicherten Stand, dazu Seriennummern, ESD-Vergaben und Kopf."""
    sns = _seriennummern_pruefen([ziel.pk, quelle.pk, vorgang.angebot_id])
    _verlauf_entfernen(ziel)  # vor dem Zurückhängen der Positionen: erst dann liegen sie noch im Ziel
    _positionen_wiederherstellen(stand["quelle_positionen"], [quelle])
    _seriennummern_zuruecksetzen(sns, stand.get("seriennummern", []), ziel)
    _esd_zurueck(stand.get("esd", []))
    quelle.refresh_from_db()
    _kopf_zurueck(quelle, stand["quelle_kopf"])
    _angebot_entfernen(vorgang.angebot, quelle, stand, meldungen)
    quelle.berechne()
    _loeschen(ziel)


def _weg_b(ziel, vorgang, quelle, stand, meldungen=None):
    """Robust zurück, wenn die Bestellung inzwischen anders geändert wurde: die Mengen werden wie beim Löschen eines Entwurfs
    zurückgebucht (fehlende Positionen neu angelegt), die Positionsnummern danach lückenlos."""
    from . import uebernahme
    sns = _seriennummern_pruefen([ziel.pk])
    quelle.refresh_from_db()
    hatte_nebenkosten = bool(quelle.versandkosten or quelle.verpackung or quelle.versicherung)
    uebernahme.beleg_zurueckbuchen(ziel, ziel_anpassen=False)
    _verlauf_entfernen(ziel)
    _seriennummern_zuruecksetzen(sns, stand.get("seriennummern", []), ziel)
    quelle.refresh_from_db()
    if "quelle_kopf" in stand and not hatte_nebenkosten:  # beleg_zurueckbuchen gibt die Werte des Ziels zurück: hier gelten die ursprünglichen
        _kopf_zurueck(quelle, stand["quelle_kopf"], erzwingen=True)
    _angebot_entfernen(vorgang.angebot, quelle, stand, meldungen)
    quelle.berechne()
    _loeschen(ziel)


def _umwandlung_zurueck(b, vorgang) -> str:
    """Umgewandelter Beleg: wieder Angebot bzw. Bestellung mit alter Nummer, altem Datum, Gültigkeit, Liefertermin und Status;
    die Positionen so wie vor der Umwandlung. Der Eintrag im Verlauf der Umwandlungen entfällt."""
    from datetime import date
    stand = vorgang.stand
    umwandlung = b.umwandlungen.order_by("-pk").first()
    if umwandlung is None or "beleg_kopf" not in stand:
        raise ValueError("Der Ausgangszustand des Belegs ist nicht mehr vorhanden – er lässt sich nicht zurückwandeln.")
    sns = _seriennummern_pruefen([b.pk])
    _positionen_wiederherstellen(stand["beleg_positionen"], [b])
    _seriennummern_zuruecksetzen(sns, stand.get("seriennummern", []))
    _esd_zurueck(stand.get("esd", []))
    kopf = stand["beleg_kopf"]

    def tag(text):
        return date.fromisoformat(text) if text else None
    b.art, b.nummer, b.belegdatum = umwandlung.von_art, umwandlung.von_nummer or None, umwandlung.von_belegdatum
    b.gueltig_bis, b.liefertermin = tag(kopf["gueltig_bis"]), tag(kopf["liefertermin"])
    b.abgeschlossen, b.automatisch_erledigt = kopf["abgeschlossen"], kopf.get("automatisch_erledigt", False)
    b.bearbeiter_id = kopf["bearbeiter_id"]
    for eintrag in serializers.deserialize("json", stand.get("kopf_felder", "[]"), ignorenonexistent=True):
        for feld in KOPFFELDER:  # Verweise, die es inzwischen nicht mehr gibt (Ansprechpartner, Zahlungsbedingung), werden gelöst
            f = Beleg._meta.get_field(feld)
            wert = getattr(eintrag.object, f.attname)
            if f.is_relation and wert and not f.related_model.objects.filter(pk=wert).exists():
                wert = None
            setattr(b, f.attname, wert)
    vorher = stand.get("vorheriger_vorgang")  # war es ein nicht gespeichertes Angebot: es ist es wieder, mit seinem eigenen Vorgang
    b.vorlaeufig, b.zuletzt_aktiv = vorher is not None, timezone.now() if vorher is not None else None
    b.save()  # nicht update(): das Änderungsprotokoll hält die Rückumwandlung fest
    umwandlung.delete()
    b.__dict__.pop("verlauf", None)
    BelegVorgang.objects.filter(beleg=b).delete()
    if vorher is not None:
        _eigenen_vorgang_zurueck(b, vorher)
    b.berechne()
    return reverse("verkauf:beleg", args=[b.pk])
