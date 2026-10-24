from apps.verkauf.mail import _Leer, anrede

from .models import Mahnstufe


def werte(mahnung) -> dict:
    b = mahnung.beleg
    komma = lambda x: f"{x:.2f}".replace(".", ",")
    return {"anrede": anrede(b), "nummer": b.nummer, "datum": b.belegdatum.strftime("%d.%m.%Y"),
            "faelligkeit": (b.faellig_am or b.belegdatum).strftime("%d.%m.%Y"), "offen": komma(mahnung.offener_betrag),
            "gebuehr": komma(mahnung.gebuehr), "gesamt": komma(mahnung.offener_betrag + mahnung.gebuehr),
            "frist": mahnung.frist.strftime("%d.%m.%Y"), "firma": b.mandant.firmenname}


def text_fuer(mahnung) -> str:
    stufe = Mahnstufe.objects.filter(mandant=mahnung.beleg.mandant, stufe=mahnung.stufe).first()
    vorlage = stufe.text if stufe and stufe.text else "{anrede},\n\nbitte begleichen Sie die Rechnung {nummer} ({gesamt} €) bis zum {frist}.\n\nMit freundlichen Grüßen\n{firma}"
    return vorlage.format_map(_Leer(werte(mahnung)))
