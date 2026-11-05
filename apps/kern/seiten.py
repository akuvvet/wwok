from django.core.paginator import Paginator

SEITENGROESSE = 50


def blaettern(request, abfrage, pro_seite=SEITENGROESSE):
    """Liefert (Seite, Querystring ohne "seite") für templates/seiten.html."""
    parameter = request.GET.copy()
    parameter.pop("seite", None)
    seite = Paginator(abfrage, pro_seite).get_page(request.GET.get("seite"))
    return seite, parameter.urlencode()
