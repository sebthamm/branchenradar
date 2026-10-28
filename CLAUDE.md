# Branchenradar / Ole & Joy

## Zwei Rechner

Sebastian arbeitet von zwei Rechnern aus. Feste Regeln:

- **Sitzungsstart:** Zuerst `git fetch` und Status prüfen. Wenn GitHub weiter ist als lokal, sofort melden und `git pull` anbieten, bevor irgendetwas geändert wird.
- **Vor jedem Deploy:** Sicherstellen, dass der lokale Stand mit GitHub übereinstimmt (`deploy.sh` bricht ab, falls nicht). Nie `--force`.
- **Sitzungsende:** Nach Änderungen ans Committen und Pushen erinnern und es anbieten (nur auf Sebastians Ausdrückliches Ja pushen).
- Keine Tokens, Passwörter oder Schlüssel in Dateien oder Gedächtnisnotizen schreiben.

## Server

- IP: 178.105.170.27
- Verzeichnis: /opt/branchenradar
- Service: branchenradar (systemd)
- URL: https://app.oleundjoy.de
- Deploy-Token steht auf dem Server in der Branchenradar-Konfiguration, nicht hier.

## Deploy-Workflow

```bash
# Immer deploy.sh verwenden, nicht manuell curl aufrufen
bash deploy.sh
```

## Lokales Verzeichnis

`C:\Users\thamm\praxis-radar\`

## GitHub

`sebthamm/branchenradar` (privat)
