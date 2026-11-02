from apps.verkauf.mail import FORMATFEHLER, _Leer, anrede, ausfuellen
from apps.verkauf.models import Mailart

from .models import Mahnstufe

STANDARD = "{anrede},\n\nbitte begleichen Sie die Rechnung {nummer} ({gesamt} €) bis zum {frist}.\n\nMit freundlichen Grüßen\n{firma}"


def werte(mahnung) -> dict:
    b = mahnung.beleg
    komma = lambda x: f"{x:.2f}".replace(".", ",")
    return {"anrede": anrede(b), "nummer": b.nummer, "datum": b.belegdatum.strftime("%d.%m.%Y"),
            "faelligkeit": (b.faellig_am or b.belegdatum).strftime("%d.%m.%Y"), "offen": komma(mahnung.offener_betrag),
            "gebuehr": komma(mahnung.gebuehr), "gesamt": komma(mahnung.offener_betrag + mahnung.gebuehr),
            "frist": mahnung.frist.strftime("%d.%m.%Y"), "firma": b.mandant.firmenname}


def text_fuer(mahnung) -> str:
    stufe = Mahnstufe.objects.filter(mandant=mahnung.beleg.mandant, stufe=mahnung.stufe).first()
    try:
        return (stufe.text if stufe and stufe.text else STANDARD).format_map(_Leer(werte(mahnung)))
    except FORMATFEHLER:  # fehlerhafte Klammern in der Mahnstufe
        return STANDARD.format_map(_Leer(werte(mahnung)))


def mail_fuer(mahnung):
    """Betreff und Text der Mahnmail; {mahntext} ist der Text der Mahnstufe, der auch im PDF steht."""
    b = mahnung.beleg
    return ausfuellen(b.mandant, Mailart.MAHNUNG, {
        **werte(mahnung), "kunde": b.kunde.name1, "bezeichnung": mahnung.bezeichnung, "mahntext": text_fuer(mahnung)})
