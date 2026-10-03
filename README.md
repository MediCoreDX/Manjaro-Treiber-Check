# Manjaro Treiber-Check

Eine native grafische Linux-App zur Hardware- und Updateprüfung unter Manjaro.
Die Oberfläche bietet ein dunkles Design, ein grünes Live-Protokoll und eine
Fortschrittsanzeige. Updates werden nur nach ausdrücklicher Bestätigung gestartet.

## Funktionen

- Erkennt PCI-Geräte, aktive Kernel-Treiber und Manjaro-`mhwd`-Profile.
- Listet angeschlossene USB-Geräte.
- Prüft parallel Updates aus Manjaros offiziellen Paketquellen, dem AUR,
  Flatpak und Snap.
- Markiert wahrscheinlich treiberbezogene Paketupdates.
- Installiert bestätigte Updates nacheinander, um Paketmanager-Sperren und
  Konflikte zu vermeiden.
- Zeigt Fortschritt und Paketmanager-Ausgaben in der App an; ein Terminalfenster
  wird nicht geöffnet.
- Startet Snap-Updates über `pkexec` und lässt Flatpak die grafische Polkit-
  Authentifizierung für systemweite Installationen verwenden, sofern erforderlich.

> **Hinweis:** AUR-Pakete führen Build-Skripte aus, die von ihren jeweiligen
> Maintainer:innen bereitgestellt werden. Installiere nur AUR-Pakete, denen du
> vertraust. Kernel- und Grafiktreiberupdates können einen Neustart erfordern.
> Die App stuft Updates nicht automatisch als zwingend oder sicherheitskritisch ein.

## Voraussetzungen zur Laufzeit

Die veröffentlichte Einzeldatei enthält Python und Tkinter. Sie ist für
**Manjaro Linux x86_64** gebaut. Für Hardware- und Updateprüfungen werden die
entsprechenden Systemwerkzeuge benötigt:

| Funktion | Werkzeug |
| --- | --- |
| Manjaro-/Repository-Pakete | `pamac` |
| AUR | `yay` oder alternativ `paru` |
| Flatpak | `flatpak` (optional) |
| Snap | `snap` (optional), `pkexec` für grafische Authentifizierung |
| PCI-Hardware | `mhwd`, `lspci` aus `pciutils` |
| USB-Hardware | `lsusb` aus `usbutils` |

Nicht installierte optionale Paketmanager werden ausgelassen. Paketinstallation
und systemweite Berechtigungen hängen von der Manjaro-/Desktop-Konfiguration ab.

## Download und Start

Lade `manjaro-treiber-check` aus dem Bereich **Releases** herunter und starte es:

```bash
chmod +x manjaro-treiber-check
./manjaro-treiber-check
```

Die App benötigt für die reine Hardware- und Updateprüfung keine
Administratorrechte. Um Updates zu installieren, klicke in der App auf
**Updates installieren** und bestätige den Dialog. Wenn ein Paketmanager
Systemrechte benötigt, erscheint der konfigurierte grafische
Authentifizierungsdialog.

## Aus dem Quellcode starten

Python 3 mit Tkinter wird benötigt:

```bash
python3 treiber_check.py
```

## Einzeldatei selbst bauen

Voraussetzungen: Python 3, `venv`, Tkinter/Tcl-Tk und Internetzugang.

```bash
./build.sh
./dist/manjaro-treiber-check
```

`build.sh` richtet eine isolierte Build-Umgebung ein und erstellt mit PyInstaller
eine ausführbare Datei unter `dist/manjaro-treiber-check`.

## Tests

```bash
python3 -m unittest -v
```

## Lizenz

Dieses Projekt steht unter der [MIT-Lizenz](LICENSE).
