---
name: wwok-feature
description: Standardablauf für jede neue Funktion oder Änderung in wwok, von der Klärung bis zur Abnahme. Bei jedem Umsetzungsauftrag verwenden.
---

# Ablauf einer Funktion

1. **Anforderung klären**: Pflichtenheft (`docs/pflichtenheft.md`), README 4/18, Mention-Screenshots (`D:\Bilder\Screenshots`), ggf. WhatsApp-Verlauf mit Oguz Özdemir. Offene Fachfragen gebündelt dem Nutzer stellen, mit eigenem Vorschlag.
2. **Kurzplan**: betroffene Dateien, Modelländerung, Fachregeln, Testfälle.
3. **Umsetzen** (Agent `wwok-entwickler`): Modell + Migration, Service, View, Template, Tests.
4. **Testen** (Skill `wwok-test`, Agent `wwok-tester`): auch Fehler- und Randfälle, lokal im Browser durchklicken.
5. **Prüfen** (Agent `wwok-reviewer`): Fachregeln, Sicherheit, Mandanten.
6. **Dokumentieren**: README Abschnitt 15/16 aktualisieren (Stand), Fachregeln in 18 nur nach Rücksprache.
7. **Commit** mit klarer deutscher Nachricht; Push und Deploy (Skill `wwok-deploy`) erst nach Freigabe des Nutzers.
