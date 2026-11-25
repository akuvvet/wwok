"""PDF/A-3 für die E-Rechnung (ZUGFeRD / Factur-X, Profil EN 16931) und eingebettete Schriften für alle PDFs.

Ausgangslage (Auftrag des Nutzers 25.11.2026): ZUGFeRD verlangt ein PDF/A-3. reportlab erzeugt mit den Standardschriften
(Helvetica) PDFs ohne eingebettete Schrift, ohne Farbprofil (OutputIntent) und ohne PDF/A-Kennung – Prüfwerkzeuge wie veraPDF
oder Mustang lehnen das ab. Hier:

1. `schriften_registrieren()` legt eine einbettbare TrueType-Schrift unter den Namen „Helvetica“, „Helvetica-Bold“ usw. ab.
   Damit betten alle PDFs (Belege, Mahnungen, Avis …) ihre Schrift ein, ohne jede Stelle einzeln umzustellen. Bevorzugt
   Liberation Sans (gleiche Laufweiten wie Helvetica/Arial, SIL OFL) aus `apps/kern/schriften/`, sonst die mit reportlab
   gelieferte Bitstream Vera (frei einbettbar).
2. `pdfa3()` ergänzt ein fertiges reportlab-PDF um Farbprofil (sRGB, mit Pillow/LittleCMS erzeugt), XMP-Metadaten mit
   PDF/A-3B-Kennung und Factur-X-Schema, passende Info-Angaben, Datei-ID und hängt die Rechnungs-XML als factur-x.xml an.
"""
import io
import os
from datetime import datetime
from xml.sax.saxutils import escape

from django.utils import timezone

SCHRIFTORDNER = os.path.join(os.path.dirname(__file__), "schriften")
# reportlab-Name → (Liberation-Datei, Vera-Datei)
SCHRIFTEN = {
    "Helvetica": ("LiberationSans-Regular.ttf", "Vera.ttf"),
    "Helvetica-Bold": ("LiberationSans-Bold.ttf", "VeraBd.ttf"),
    "Helvetica-Oblique": ("LiberationSans-Italic.ttf", "VeraIt.ttf"),
    "Helvetica-BoldOblique": ("LiberationSans-BoldItalic.ttf", "VeraBI.ttf"),
}
_registriert = False


def schriftdatei(liberation: str, vera: str) -> str:
    eigene = os.path.join(SCHRIFTORDNER, liberation)
    if os.path.exists(eigene):
        return eigene
    import reportlab
    return os.path.join(os.path.dirname(reportlab.__file__), "fonts", vera)


def schriften_registrieren():
    """Einmal je Prozess (AppConfig.ready): eingebettete TrueType-Schrift statt der nicht eingebetteten Standard-Helvetica."""
    global _registriert
    if _registriert:
        return
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for name, (liberation, vera) in SCHRIFTEN.items():
        pdfmetrics.registerFont(TTFont(name, schriftdatei(liberation, vera)))
    for fett, kursiv, name in ((0, 0, "Helvetica"), (1, 0, "Helvetica-Bold"), (0, 1, "Helvetica-Oblique"), (1, 1, "Helvetica-BoldOblique")):
        addMapping("Helvetica", fett, kursiv, name)  # <b>/<i> in Absätzen
    _registriert = True


def srgb_profil() -> bytes:
    """sRGB-Farbprofil (ICC) für den OutputIntent, ohne fremde Datei: mit LittleCMS aus Pillow erzeugt."""
    from PIL import ImageCms
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _pdf_datum(zeit: datetime) -> str:
    versatz = zeit.strftime("%z") or "+0000"
    return f"D:{zeit:%Y%m%d%H%M%S}{versatz[:3]}'{versatz[3:]}'"


def _xmp_datum(zeit: datetime) -> str:
    versatz = zeit.strftime("%z") or "+0000"
    return f"{zeit:%Y-%m-%dT%H:%M:%S}{versatz[:3]}:{versatz[3:]}"


def xmp(titel: str, autor: str, zeit: datetime, mit_facturx: bool) -> bytes:
    """XMP mit PDF/A-3B-Kennung; mit E-Rechnung auch das Factur-X-Schema samt der für PDF/A nötigen Schemabeschreibung."""
    t, a, d = escape(titel), escape(autor), _xmp_datum(zeit)
    facturx = ""
    if mit_facturx:
        facturx = """
  <rdf:Description rdf:about="" xmlns:fx="urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#">
   <fx:DocumentType>INVOICE</fx:DocumentType>
   <fx:DocumentFileName>factur-x.xml</fx:DocumentFileName>
   <fx:Version>1.0</fx:Version>
   <fx:ConformanceLevel>EN 16931</fx:ConformanceLevel>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:pdfaExtension="http://www.aiim.org/pdfa/ns/extension/"
      xmlns:pdfaSchema="http://www.aiim.org/pdfa/ns/schema#" xmlns:pdfaProperty="http://www.aiim.org/pdfa/ns/property#">
   <pdfaExtension:schemas>
    <rdf:Bag>
     <rdf:li rdf:parseType="Resource">
      <pdfaSchema:schema>Factur-X PDFA Extension Schema</pdfaSchema:schema>
      <pdfaSchema:namespaceURI>urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#</pdfaSchema:namespaceURI>
      <pdfaSchema:prefix>fx</pdfaSchema:prefix>
      <pdfaSchema:property>
       <rdf:Seq>
        <rdf:li rdf:parseType="Resource"><pdfaProperty:name>DocumentFileName</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>name of the embedded XML invoice file</pdfaProperty:description></rdf:li>
        <rdf:li rdf:parseType="Resource"><pdfaProperty:name>DocumentType</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>INVOICE</pdfaProperty:description></rdf:li>
        <rdf:li rdf:parseType="Resource"><pdfaProperty:name>Version</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>The actual version of the Factur-X XML schema</pdfaProperty:description></rdf:li>
        <rdf:li rdf:parseType="Resource"><pdfaProperty:name>ConformanceLevel</pdfaProperty:name><pdfaProperty:valueType>Text</pdfaProperty:valueType><pdfaProperty:category>external</pdfaProperty:category><pdfaProperty:description>The conformance level of the embedded Factur-X data</pdfaProperty:description></rdf:li>
       </rdf:Seq>
      </pdfaSchema:property>
     </rdf:li>
    </rdf:Bag>
   </pdfaExtension:schemas>
  </rdf:Description>"""
    text = f"""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">
   <pdfaid:part>3</pdfaid:part>
   <pdfaid:conformance>B</pdfaid:conformance>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:format>application/pdf</dc:format>
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{t}</rdf:li></rdf:Alt></dc:title>
   <dc:creator><rdf:Seq><rdf:li>{a}</rdf:li></rdf:Seq></dc:creator>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">
   <xmp:CreatorTool>wwok</xmp:CreatorTool>
   <xmp:CreateDate>{d}</xmp:CreateDate>
   <xmp:ModifyDate>{d}</xmp:ModifyDate>
   <xmp:MetadataDate>{d}</xmp:MetadataDate>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:pdf="http://ns.adobe.com/pdf/1.3/">
   <pdf:Producer>wwok (reportlab, pypdf)</pdf:Producer>
  </rdf:Description>{facturx}
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    return text.encode("utf-8")


def pdfa3(pdf: bytes, titel: str, autor: str, xml: bytes | None = None, zeit: datetime | None = None) -> bytes:
    """PDF/A-3B aus einem reportlab-PDF; mit `xml` als ZUGFeRD/Factur-X (factur-x.xml, Beziehung „Data“)."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (ArrayObject, ByteStringObject, DecodedStreamObject, DictionaryObject, NameObject,
                               NumberObject, TextStringObject)

    zeit = timezone.localtime(zeit or timezone.now())
    schreiber = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    schreiber.pdf_header = b"%PDF-1.7"
    wurzel = schreiber._root_object

    # Farbprofil: OutputIntent sRGB (Pflicht, sobald Farben ohne Profil verwendet werden)
    profil = DecodedStreamObject()
    profil.set_data(srgb_profil())
    profil[NameObject("/N")] = NumberObject(3)
    absicht = DictionaryObject({
        NameObject("/Type"): NameObject("/OutputIntent"), NameObject("/S"): NameObject("/GTS_PDFA1"),
        NameObject("/OutputConditionIdentifier"): TextStringObject("sRGB IEC61966-2.1"),
        NameObject("/Info"): TextStringObject("sRGB IEC61966-2.1"),
        NameObject("/DestOutputProfile"): schreiber._add_object(profil)})
    wurzel[NameObject("/OutputIntents")] = ArrayObject([schreiber._add_object(absicht)])

    if xml is not None:
        schreiber.add_attachment("factur-x.xml", xml)
        spec = wurzel["/Names"]["/EmbeddedFiles"]["/Names"][-1]
        spec_obj = spec.get_object()
        spec_obj[NameObject("/AFRelationship")] = NameObject("/Data")
        spec_obj[NameObject("/UF")] = TextStringObject("factur-x.xml")
        spec_obj[NameObject("/Desc")] = TextStringObject("Factur-X/ZUGFeRD Rechnungsdaten")
        datei = spec_obj["/EF"]["/F"].get_object()
        datei[NameObject("/Subtype")] = NameObject("/text/xml")  # pypdf schreibt den Schrägstrich selbst als #2F
        datei[NameObject("/Params")] = DictionaryObject({
            NameObject("/ModDate"): TextStringObject(_pdf_datum(zeit)), NameObject("/Size"): NumberObject(len(xml))})
        wurzel[NameObject("/AF")] = ArrayObject([spec])

    meta = DecodedStreamObject()
    meta.set_data(xmp(titel, autor, zeit, xml is not None))
    meta[NameObject("/Type")] = NameObject("/Metadata")
    meta[NameObject("/Subtype")] = NameObject("/XML")
    wurzel[NameObject("/Metadata")] = schreiber._add_object(meta)

    # Info-Angaben müssen zu den XMP-Werten passen (PDF/A)
    schreiber.add_metadata({"/Title": titel, "/Author": autor, "/Creator": "wwok", "/Producer": "wwok (reportlab, pypdf)",
                            "/CreationDate": _pdf_datum(zeit), "/ModDate": _pdf_datum(zeit)})
    info = schreiber._info.get_object() if schreiber._info is not None else None
    for schluessel in ("/Subject", "/Keywords", "/Trapped"):
        if info is not None and schluessel in info:
            del info[schluessel]
    kennung = ByteStringObject(os.urandom(16))  # Datei-ID im Trailer (PDF/A)
    schreiber._ID = ArrayObject([kennung, kennung])
    ausgabe = io.BytesIO()
    schreiber.write(ausgabe)
    return ausgabe.getvalue()


def pruefen(pdf: bytes) -> list:
    """Eigene Strukturprüfung der wichtigsten PDF/A-3-Regeln (ersetzt keinen Validator wie veraPDF, fängt aber Rückschritte in
    den Tests ab): alle Schriften eingebettet, Farbprofil, PDF/A-Kennung im XMP, Datei-ID, keine Verschlüsselung, Anhänge mit
    AFRelationship. Gibt die gefundenen Fehler zurück (leer = in Ordnung)."""
    from pypdf import PdfReader
    leser = PdfReader(io.BytesIO(pdf))
    fehler = []
    if leser.is_encrypted:
        fehler.append("verschlüsselt")
    gesehen = set()

    def schriften(ressourcen):
        if not ressourcen:
            return
        ressourcen = ressourcen.get_object()
        for schrift in (ressourcen.get("/Font") or {}).values():
            schrift = schrift.get_object()
            if id(schrift) in gesehen:
                continue
            gesehen.add(id(schrift))
            teile = [d.get_object() for d in schrift.get("/DescendantFonts", [])] or [schrift]
            for teil in teile:
                beschreibung = teil.get("/FontDescriptor")
                beschreibung = beschreibung.get_object() if beschreibung else {}
                if not any(k in beschreibung for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                    fehler.append(f"Schrift nicht eingebettet: {teil.get('/BaseFont')}")
        for objekt in (ressourcen.get("/XObject") or {}).values():
            objekt = objekt.get_object()
            if objekt.get("/Subtype") == "/Form":
                schriften(objekt.get("/Resources"))

    for seite in leser.pages:
        schriften(seite.get("/Resources"))
    wurzel = leser.trailer["/Root"]
    absichten = wurzel.get("/OutputIntents")
    if not absichten or not any(a.get_object().get("/DestOutputProfile") for a in absichten):
        fehler.append("kein Farbprofil (OutputIntent)")
    meta = wurzel.get("/Metadata")
    text = meta.get_object().get_data().decode("utf-8", "replace") if meta else ""
    if "<pdfaid:part>3</pdfaid:part>" not in text:
        fehler.append("keine PDF/A-3-Kennung im XMP")
    if "/ID" not in leser.trailer:
        fehler.append("keine Datei-ID")
    for spec in (wurzel.get("/AF") or []):
        spec = spec.get_object()
        if "/AFRelationship" not in spec or "/UF" not in spec:
            fehler.append(f"Anhang ohne AFRelationship/UF: {spec.get('/F')}")
        elif "/Subtype" not in spec["/EF"]["/F"].get_object():
            fehler.append("Anhang ohne MIME-Typ")
    return fehler


def anhang(pdf: bytes, name: str = "factur-x.xml") -> bytes | None:
    """Inhalt eines eingebetteten Anhangs (z. B. der Rechnungs-XML) oder None."""
    from pypdf import PdfReader
    inhalte = PdfReader(io.BytesIO(pdf)).attachments.get(name)
    return inhalte[0] if inhalte else None
