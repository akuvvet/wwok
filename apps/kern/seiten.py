"""Blättern für alle Tabellen: eine Funktion für die eigenen Listen (blaettern + templates/seiten.html) und ein Mixin für die
Listen der Verwaltung (Django-Admin, templates/admin/pagination.html). Die gewählte Anzahl pro Seite gilt je Liste und
bleibt in der Sitzung gemerkt (Wunsch des Nutzers 20.11.2026)."""
from django.contrib.admin.views.main import PAGE_VAR
from django.core.paginator import Paginator

SEITENGROESSE = 50
SEITENGROESSEN = (25, 50, 100, 200, 500)
PARAMETER = "pro_seite"


def _liste(request, schluessel=None):
    if schluessel:
        return schluessel
    treffer = getattr(request, "resolver_match", None)
    return treffer.view_name if treffer else request.path


def pro_seite(request, standard=SEITENGROESSE, schluessel=None) -> int:
    """Anzahl pro Seite: ?pro_seite=… (wird je Liste in der Sitzung gemerkt), sonst die gemerkte, sonst der Standard der Liste."""
    merk = f"pro_seite:{_liste(request, schluessel)}"
    wert = request.GET.get(PARAMETER, "")
    if wert.isdigit() and int(wert) in SEITENGROESSEN:
        request.session[merk] = int(wert)
        return int(wert)
    gemerkt = request.session.get(merk)
    return gemerkt if gemerkt in SEITENGROESSEN else standard


def blaettern(request, abfrage, pro_seite_standard=SEITENGROESSE, schluessel=None):
    """Liefert (Seite, Querystring ohne „seite“/„pro_seite“) für templates/seiten.html. Die Seite kennt dazu ihre Anzahl
    pro Seite und die wählbaren Größen."""
    parameter = request.GET.copy()
    parameter.pop("seite", None)
    parameter.pop(PARAMETER, None)
    groesse = pro_seite(request, pro_seite_standard, schluessel)
    seite = Paginator(abfrage, groesse).get_page(request.GET.get("seite"))
    seite.pro_seite, seite.groessen = groesse, SEITENGROESSEN
    return seite, parameter.urlencode()


class BlaetternAdminMixin:
    """Verwaltungslisten mit derselben Blätterleiste wie die übrigen Tabellen und wählbarer Anzahl pro Seite.
    „pro_seite“ wird vor der Admin-Liste aus der Anfrage genommen (sie hielte es sonst für einen Filter) und gemerkt."""

    def changelist_view(self, request, extra_context=None):
        groesse = pro_seite(request, SEITENGROESSE, schluessel=f"admin:{self.opts.label_lower}")
        if PARAMETER in request.GET:
            get = request.GET.copy()
            get.pop(PARAMETER)
            get.pop(PAGE_VAR, None)  # neue Größe: zurück auf Seite 1
            request.GET = get
            request.META["QUERY_STRING"] = get.urlencode()
        request.wwok_pro_seite = groesse
        return super().changelist_view(request, extra_context)

    def get_changelist(self, request, **kwargs):
        basis = super().get_changelist(request, **kwargs)
        groesse = getattr(request, "wwok_pro_seite", None) or self.list_per_page

        class WwokChangeList(basis):
            def __init__(self, *args, **kw):
                if "list_per_page" in kw:
                    kw["list_per_page"] = groesse
                else:  # Reihenfolge wie ChangeList.__init__: request, model, list_display, … , list_per_page (Nr. 9)
                    args = list(args)
                    args[8] = groesse
                super().__init__(*args, **kw)
                self.pro_seite, self.groessen = groesse, SEITENGROESSEN
                # Werte für die Blätterleiste wie in templates/seiten.html (Seiten zählen ab 1)
                seiten, nr = self.paginator.num_pages, self.page_num
                self.seiten_anzahl, self.seiten_nr = seiten, nr
                self.von = (nr - 1) * self.list_per_page + 1 if self.result_count else 0
                self.bis = min(nr * self.list_per_page, self.result_count)
                link = lambda n: self.get_query_string({PAGE_VAR: n})  # noqa: E731
                self.link_erste, self.link_zurueck = (link(1), link(nr - 1)) if nr > 1 else (None, None)
                self.link_weiter, self.link_letzte = (link(nr + 1), link(seiten)) if nr < seiten else (None, None)
                self.link_groesse = self.get_query_string(remove=[PAGE_VAR])

        return WwokChangeList
