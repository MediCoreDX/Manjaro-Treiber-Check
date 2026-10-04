#!/usr/bin/env python3
"""Hardware and package update checker for Manjaro Linux."""

from __future__ import annotations

import codecs
from concurrent.futures import ThreadPoolExecutor
import os
import platform
import queue
import re
import select
import shutil
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Callable


DRIVER_PACKAGE = re.compile(
    r"^(?:linux(?:[0-9].*)?|linux-firmware(?:[-\w.]*)?|"
    r"nvidia(?:[-\w.]*)?|dkms|mesa(?:[-\w.]*)?|"
    r"vulkan(?:[-\w.]*)?|xf86-video(?:[-\w.]*)?|"
    r"(?:intel|amd)-ucode)$",
    re.IGNORECASE,
)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


class CheckError(RuntimeError):
    """A system inspection command failed."""


@dataclass(frozen=True)
class Device:
    name: str
    driver: str | None = None
    modules: str | None = None


@dataclass(frozen=True)
class ScanResult:
    kernel: str
    pci_devices: list[Device]
    usb_devices: list[str]
    mhwd_profiles: str
    updates: list[str]
    errors: list[str]


def build_update_jobs(pamac: str) -> list[tuple[str, list[str]]]:
    jobs = [
        (
            "Manjaro-/Pacman- und AUR-Pakete (Pamac)",
            [pamac, "upgrade", "--aur", "--no-confirm"],
        )
    ]
    flatpak = shutil.which("flatpak")
    if flatpak:
        # Keep Polkit authorization available for system-wide Flatpak installations.
        jobs.append(("Flatpak", [flatpak, "update", "--assumeyes"]))
    snap = shutil.which("snap")
    pkexec = shutil.which("pkexec")
    if snap and pkexec:
        jobs.append(("Snap", [pkexec, snap, "refresh"]))
    elif snap:
        jobs.append(
            (
                "Snap",
                [snap, "refresh"],
            )
        )
    return jobs


def run_command(
    arguments: list[str],
    accepted_codes: tuple[int, ...] = (0,),
    empty_success_codes: tuple[int, ...] = (),
) -> str:
    executable = shutil.which(arguments[0])
    if executable is None:
        raise CheckError(f"Befehl nicht gefunden: {arguments[0]}")
    try:
        result = subprocess.run(
            [executable, *arguments[1:]],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"Zeitüberschreitung bei {arguments[0]}.") from exc
    except OSError as exc:
        raise CheckError(f"{arguments[0]} konnte nicht gestartet werden: {exc}") from exc
    if result.returncode not in accepted_codes:
        if (
            result.returncode in empty_success_codes
            and not result.stdout.strip()
            and not result.stderr.strip()
        ):
            return ""
        detail = (result.stderr or result.stdout).strip()
        suffix = f" {detail}" if detail else ""
        raise CheckError(f"{arguments[0]} wurde mit Status {result.returncode} beendet.{suffix}")
    return result.stdout.strip()


def parse_pci_devices(output: str) -> list[Device]:
    devices: list[Device] = []
    current_name: str | None = None
    current_driver: str | None = None
    current_modules: str | None = None

    def save_device() -> None:
        if current_name is not None:
            devices.append(Device(current_name, current_driver, current_modules))

    for line in output.splitlines():
        if not line[:1].isspace() and re.match(r"^[0-9a-fA-F:.]+\s", line):
            save_device()
            current_name = line.split(": ", 1)[-1].strip()
            current_driver = None
            current_modules = None
        elif line.strip().startswith("Kernel driver in use:"):
            current_driver = line.split(":", 1)[1].strip()
        elif line.strip().startswith("Kernel modules:"):
            current_modules = line.split(":", 1)[1].strip()
    save_device()
    return devices


def parse_usb_devices(output: str) -> list[str]:
    devices = []
    for line in output.splitlines():
        if "Linux Foundation" in line and "root hub" in line:
            continue
        match = re.search(r"ID\s+[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\s+(.+)$", line)
        if match:
            devices.append(match.group(1).strip())
    return devices


def parse_updates(output: str) -> list[str]:
    updates = []
    for line in ANSI_ESCAPE.sub("", output).splitlines():
        line = line.strip()
        normalized = line.casefold().rstrip(".")
        if (
            line
            and normalized not in {"all snaps up to date", "there is nothing to do", "no updates"}
            and not normalized.startswith("all snaps up to date")
            and not normalized.startswith("there is nothing to do")
            and not line.casefold().startswith("name ")
        ):
            updates.append(line)
    return updates


def update_is_driver_related(update: str) -> bool:
    package = update.split("] ", 1)[-1]
    package_name = package.split()[0].rstrip(":")
    return bool(DRIVER_PACKAGE.match(package_name))


def scan_system(progress: Callable[[str], None] | None = None) -> ScanResult:
    if platform.system() != "Linux":
        raise CheckError("Diese Version der App unterstützt Manjaro Linux.")

    errors: list[str] = []

    def inspect(label: str, action):
        if progress:
            progress(f"Prüfe {label} …")
        try:
            result = action()
        except CheckError as exc:
            errors.append(f"{label}: {exc}")
            if progress:
                progress(f"FEHLER: {label}: {exc}")
            return None
        if progress:
            progress(f"OK: {label}")
        return result

    checks: list[tuple[str, Callable[[], str]]] = [
        ("Kernel", lambda: run_command(["uname", "-r"])),
        ("PCI-Hardware", lambda: run_command(["lspci", "-nnk"])),
        ("USB-Hardware", lambda: run_command(["lsusb"])),
        ("Manjaro-Treiberprofile", lambda: run_command(["mhwd", "-li"])),
        (
            "Updates Manjaro/Pacman",
            lambda: run_command(
                ["pamac", "checkupdates", "--no-aur", "--quiet"],
                accepted_codes=(0, 100),
            ),
        ),
    ]
    aur_manager = shutil.which("yay") or shutil.which("paru")
    if aur_manager:
        checks.append(
            (
                "Updates AUR",
                lambda: run_command([aur_manager, "-Qua"], empty_success_codes=(1,)),
            )
        )
    if flatpak := shutil.which("flatpak"):
        checks.append(
            (
                "Updates Flatpak",
                lambda: run_command(
                    [flatpak, "remote-ls", "--updates", "--columns=application,version"]
                ),
            )
        )
    if snap := shutil.which("snap"):
        checks.append(
            (
                "Updates Snap",
                lambda: run_command([snap, "refresh", "--list"]),
            )
        )

    with ThreadPoolExecutor(
        max_workers=len(checks), thread_name_prefix="driver-check"
    ) as executor:
        futures = {
            name: executor.submit(inspect, name, action)
            for name, action in checks
        }
        results = {name: future.result() for name, future in futures.items()}

    kernel = results.get("Kernel") or "unbekannt"
    pci_output = results.get("PCI-Hardware") or ""
    usb_output = results.get("USB-Hardware") or ""
    mhwd_profiles = results.get("Manjaro-Treiberprofile") or ""
    update_output = results.get("Updates Manjaro/Pacman")
    updates = [
        f"[Manjaro/Pacman] {item}"
        for item in parse_updates(update_output or "")
    ]

    if aur_manager:
        updates.extend(
            f"[AUR] {item}"
            for item in parse_updates(results.get("Updates AUR") or "")
        )
    if "Updates Flatpak" in results:
        updates.extend(
            f"[Flatpak] {item}"
            for item in parse_updates(results.get("Updates Flatpak") or "")
        )
    if "Updates Snap" in results:
        updates.extend(
            f"[Snap] {item}"
            for item in parse_updates(results.get("Updates Snap") or "")
        )

    return ScanResult(
        kernel=kernel,
        pci_devices=parse_pci_devices(pci_output),
        usb_devices=parse_usb_devices(usb_output),
        mhwd_profiles=mhwd_profiles,
        updates=updates,
        errors=errors,
    )


class DriverCheckerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Manjaro Treiber-Check")
        self.root.geometry("1000x740")
        self.root.minsize(760, 580)
        self.results: queue.Queue[ScanResult | Exception] = queue.Queue()
        self.activity_queue: queue.Queue[str] = queue.Queue()
        self.update_results: queue.Queue[list[tuple[str, int | Exception]]] = queue.Queue()
        self.operation_mode: str | None = None
        self.console_line_count = 0

        self.colors = {
            "background": "#111821",
            "surface": "#1a2431",
            "card": "#202d3c",
            "ink": "#edf3fa",
            "muted": "#9aaabd",
            "line": "#344356",
            "accent": "#48cf88",
            "accent_dark": "#2eaa6a",
            "blue": "#82b4ff",
            "warning": "#ffca76",
            "warning_bg": "#392f21",
            "good": "#7de0a3",
            "good_bg": "#20372d",
            "blue_bg": "#1e2e43",
            "subtle": "#263444",
            "subtle_active": "#2d3c4e",
            "selected": "#294838",
        }
        self.root.configure(background=self.colors["background"])
        self.configure_styles()

        shell = ttk.Frame(root, style="App.TFrame", padding=(28, 24, 28, 24))
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell, style="App.TFrame")
        header.pack(fill="x", pady=(0, 20))

        brand = ttk.Frame(header, style="App.TFrame")
        brand.pack(side="left", fill="x", expand=True)
        ttk.Label(
            brand, text="SYSTEMWERKZEUGE  /  MANJARO", style="Eyebrow.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            brand, text="Treiber-Check", style="Title.TLabel"
        ).pack(anchor="w", pady=(3, 2))
        ttk.Label(
            brand,
            text="Hardware und verfügbare Updates auf einen Blick",
            style="Subtitle.TLabel",
        ).pack(anchor="w")

        actions = ttk.Frame(header, style="App.TFrame")
        actions.pack(side="right", anchor="center")
        self.status_dot = ttk.Label(actions, text="●", style="StatusIdle.TLabel")
        self.status_dot.pack(side="left", padx=(0, 8))
        self.status = ttk.Label(actions, text="Noch nicht geprüft", style="StatusIdle.TLabel")
        self.status.pack(side="left", padx=(0, 14))
        self.scan_button = ttk.Button(
            actions,
            text="  Jetzt prüfen  ",
            style="Accent.TButton",
            command=self.start_scan,
        )
        self.scan_button.pack(side="left")

        cards = ttk.Frame(shell, style="App.TFrame")
        cards.pack(fill="x", pady=(0, 18))
        for column in range(3):
            cards.columnconfigure(column, weight=1, uniform="summary")
        self.summary_values: dict[str, ttk.Label] = {}
        self.summary_notes: dict[str, ttk.Label] = {}
        self.create_summary_card(cards, 0, "PCI-GERÄTE", "pci", "Erkannte Komponenten")
        self.create_summary_card(cards, 1, "USB-GERÄTE", "usb", "Angeschlossene Geräte")
        self.create_summary_card(cards, 2, "UPDATES", "updates", "Davon treiberrelevant")

        self.notebook = ttk.Notebook(shell, style="Modern.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        overview = ttk.Frame(self.notebook, style="Page.TFrame", padding=22)
        hardware = ttk.Frame(self.notebook, style="Page.TFrame", padding=16)
        details = ttk.Frame(self.notebook, style="Page.TFrame", padding=20)
        self.notebook.add(overview, text="  Übersicht  ")
        self.notebook.add(hardware, text="  Hardware  ")
        self.notebook.add(details, text="  Treiberprofile & Hinweise  ")
        self.build_overview(overview)
        self.build_hardware(hardware)
        self.build_details(details)

    def configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background=self.colors["background"])
        style.configure("Page.TFrame", background=self.colors["surface"])
        style.configure(
            "Card.TFrame",
            background=self.colors["card"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Eyebrow.TLabel",
            background=self.colors["background"],
            foreground=self.colors["accent_dark"],
            font=("TkDefaultFont", 9, "bold"),
        )
        style.configure(
            "Title.TLabel",
            background=self.colors["background"],
            foreground=self.colors["ink"],
            font=("TkDefaultFont", 25, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=self.colors["background"],
            foreground=self.colors["muted"],
            font=("TkDefaultFont", 10),
        )
        style.configure(
            "StatusIdle.TLabel",
            background=self.colors["background"],
            foreground=self.colors["muted"],
            font=("TkDefaultFont", 9),
        )
        style.configure(
            "StatusGood.TLabel",
            background=self.colors["background"],
            foreground=self.colors["good"],
            font=("TkDefaultFont", 9, "bold"),
        )
        style.configure(
            "StatusWarning.TLabel",
            background=self.colors["background"],
            foreground=self.colors["warning"],
            font=("TkDefaultFont", 9, "bold"),
        )
        style.configure(
            "Accent.TButton",
            background=self.colors["accent"],
            foreground="#102118",
            borderwidth=0,
            padding=(12, 9),
            font=("TkDefaultFont", 10, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", self.colors["accent_dark"]), ("disabled", "#465263")],
            foreground=[("disabled", "#aab5c2")],
        )
        style.configure(
            "Secondary.TButton",
            background=self.colors["subtle"],
            foreground=self.colors["ink"],
            borderwidth=1,
            padding=(12, 9),
            font=("TkDefaultFont", 9, "bold"),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", self.colors["subtle_active"])],
            foreground=[("active", self.colors["ink"])],
        )
        style.configure(
            "Modern.TNotebook",
            background=self.colors["background"],
            borderwidth=0,
            tabmargins=(0, 0, 8, 0),
        )
        style.configure(
            "Modern.TNotebook.Tab",
            background=self.colors["subtle"],
            foreground=self.colors["muted"],
            padding=(15, 10),
            font=("TkDefaultFont", 9, "bold"),
        )
        style.map(
            "Modern.TNotebook.Tab",
            background=[
                ("selected", self.colors["surface"]),
                ("active", self.colors["subtle_active"]),
            ],
            foreground=[("selected", self.colors["accent"])],
        )
        style.configure(
            "Modern.Treeview",
            background=self.colors["surface"],
            fieldbackground=self.colors["surface"],
            foreground=self.colors["ink"],
            rowheight=36,
            borderwidth=0,
            font=("TkDefaultFont", 9),
        )
        style.configure(
            "Modern.Treeview.Heading",
            background=self.colors["card"],
            foreground=self.colors["muted"],
            relief="flat",
            padding=(10, 10),
            font=("TkDefaultFont", 9, "bold"),
        )
        style.map(
            "Modern.Treeview",
            background=[("selected", self.colors["selected"])],
            foreground=[("selected", self.colors["ink"])],
        )
        style.configure(
            "Modern.Vertical.TScrollbar",
            background=self.colors["subtle_active"],
            troughcolor=self.colors["background"],
            borderwidth=0,
            arrowsize=12,
        )
        style.configure(
            "Dark.Horizontal.TProgressbar",
            background=self.colors["accent"],
            troughcolor="#030604",
            bordercolor="#030604",
            lightcolor=self.colors["accent"],
            darkcolor=self.colors["accent_dark"],
        )

    def create_summary_card(
        self, parent: ttk.Frame, column: int, title: str, key: str, note: str
    ) -> None:
        card = ttk.Frame(parent, style="Card.TFrame", padding=(16, 13))
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
        ttk.Label(
            card,
            text=title,
            background=self.colors["card"],
            foreground=self.colors["muted"],
            font=("TkDefaultFont", 8, "bold"),
        ).pack(anchor="w")
        value = ttk.Label(
            card,
            text="—",
            background=self.colors["card"],
            foreground=self.colors["ink"],
            font=("TkDefaultFont", 23, "bold"),
        )
        value.pack(anchor="w", pady=(4, 0))
        subtext = ttk.Label(
            card,
            text=note,
            background=self.colors["card"],
            foreground=self.colors["muted"],
            font=("TkDefaultFont", 9),
        )
        subtext.pack(anchor="w", pady=(1, 0))
        self.summary_values[key] = value
        self.summary_notes[key] = subtext

    def build_overview(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent, text="Aktualisierungsstatus", background=self.colors["surface"],
            foreground=self.colors["ink"], font=("TkDefaultFont", 15, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            parent,
            text="Prüft Manjaro-/Pacman-Pakete, AUR, Flatpak und Snap. "
            "Installiert wird nur nach deiner Bestätigung.",
            background=self.colors["surface"],
            foreground=self.colors["muted"],
            wraplength=780,
            justify="left",
        ).pack(anchor="w", pady=(5, 15))
        self.update_banner = tk.Label(
            parent,
            text="Starten Sie eine Prüfung, um den Update-Status abzurufen.",
            anchor="w",
            justify="left",
            wraplength=780,
            background=self.colors["subtle"],
            foreground=self.colors["ink"],
            padx=15,
            pady=14,
            font=("TkDefaultFont", 10),
        )
        self.update_banner.pack(fill="x")
        update_actions = ttk.Frame(parent, style="Page.TFrame")
        update_actions.pack(fill="x", pady=(12, 0))
        self.install_button = ttk.Button(
            update_actions,
            text="Updates installieren",
            style="Secondary.TButton",
            command=self.open_update_manager,
        )
        self.install_button.pack(side="left")
        ttk.Label(
            update_actions,
            text="Pacman/AUR, Flatpak und Snap · Fortschritt hier im Fenster",
            background=self.colors["surface"],
            foreground=self.colors["muted"],
            font=("TkDefaultFont", 9),
        ).pack(side="left", padx=(12, 0))
        self.kernel_label = ttk.Label(
            parent,
            text="Laufender Kernel  ·  —",
            background=self.colors["surface"],
            foreground=self.colors["muted"],
            font=("TkDefaultFont", 9),
        )
        self.kernel_label.pack(anchor="w", pady=(18, 6))
        ttk.Label(
            parent,
            text="Hinweis: Kernel- und Grafiktreiberupdates können einen Neustart erfordern. "
            "AUR-Pakete führen fremde Build-Skripte aus. Ob ein Update zwingend ist, "
            "wird nicht automatisch bewertet.",
            background=self.colors["surface"],
            foreground=self.colors["muted"],
            wraplength=780,
            justify="left",
            font=("TkDefaultFont", 9),
        ).pack(anchor="w", pady=(7, 0))
        self.progress = ttk.Progressbar(
            parent, mode="indeterminate", style="Dark.Horizontal.TProgressbar"
        )
        self.progress.pack(fill="x", pady=(12, 6))
        ttk.Label(
            parent,
            text="AKTIVITÄT  /  LIVE-PROTOKOLL",
            background=self.colors["surface"],
            foreground=self.colors["muted"],
            font=("TkDefaultFont", 8, "bold"),
        ).pack(anchor="w", pady=(5, 5))
        console_frame = ttk.Frame(parent, style="Page.TFrame")
        console_frame.pack(fill="both", expand=True)
        self.console = tk.Text(
            console_frame,
            height=7,
            wrap="word",
            state="disabled",
            background="#030604",
            foreground="#55ff72",
            insertbackground="#55ff72",
            selectbackground="#164a21",
            relief="flat",
            padx=12,
            pady=9,
            font=("TkFixedFont", 9),
        )
        console_scrollbar = ttk.Scrollbar(
            console_frame,
            orient="vertical",
            command=self.console.yview,
            style="Modern.Vertical.TScrollbar",
        )
        self.console.configure(yscrollcommand=console_scrollbar.set)
        self.console.pack(side="left", fill="both", expand=True)
        console_scrollbar.pack(side="right", fill="y")
        self.log_activity("Bereit. Starte eine Prüfung oder installiere Updates.")

    def build_hardware(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent, text="Erkannte Hardware", background=self.colors["surface"],
            foreground=self.colors["ink"], font=("TkDefaultFont", 14, "bold"),
        ).pack(anchor="w", pady=(0, 12))
        table_frame = ttk.Frame(parent, style="Page.TFrame")
        table_frame.pack(fill="both", expand=True)
        self.device_table = ttk.Treeview(
            table_frame,
            columns=("type", "device", "driver"),
            show="headings",
            style="Modern.Treeview",
        )
        self.device_table.heading("type", text="TYP")
        self.device_table.heading("device", text="GERÄT")
        self.device_table.heading("driver", text="TREIBER / STATUS")
        self.device_table.column("type", width=90, minwidth=80, stretch=False)
        self.device_table.column("device", width=480, minwidth=180)
        self.device_table.column("driver", width=210, minwidth=140)
        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.device_table.yview,
            style="Modern.Vertical.TScrollbar",
        )
        self.device_table.configure(yscrollcommand=scrollbar.set)
        self.device_table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.device_table.tag_configure("unbound", foreground=self.colors["warning"])

    def build_details(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent, text="Manjaro-Treiberprofile", background=self.colors["surface"],
            foreground=self.colors["ink"], font=("TkDefaultFont", 14, "bold"),
        ).pack(anchor="w")
        self.profile_text = tk.Text(
            parent,
            height=8,
            wrap="word",
            state="disabled",
            background=self.colors["background"],
            foreground=self.colors["ink"],
            insertbackground=self.colors["ink"],
            relief="flat",
            padx=12,
            pady=10,
            font=("TkFixedFont", 9),
        )
        self.profile_text.pack(fill="x", pady=(10, 20))
        ttk.Label(
            parent, text="Prüfhinweise", background=self.colors["surface"],
            foreground=self.colors["ink"], font=("TkDefaultFont", 14, "bold"),
        ).pack(anchor="w")
        self.errors_text = tk.Text(
            parent,
            height=6,
            wrap="word",
            state="disabled",
            background=self.colors["background"],
            foreground=self.colors["warning"],
            insertbackground=self.colors["ink"],
            relief="flat",
            padx=12,
            pady=10,
            font=("TkDefaultFont", 9),
        )
        self.errors_text.pack(fill="both", expand=True, pady=(10, 0))

    def start_scan(self) -> None:
        if self.operation_mode is not None:
            return
        self.scan_button.configure(state="disabled")
        self.install_button.configure(state="disabled")
        self.status.configure(text="Prüfe Hardware und Paketquellen …", style="StatusIdle.TLabel")
        self.status_dot.configure(foreground=self.colors["blue"])
        self.operation_mode = "scan"
        self.progress.configure(mode="indeterminate")
        self.progress.start(100)
        self.clear_console()
        self.log_activity("Starte Hardware- und Paketquellenprüfung.")
        self.update_banner.configure(
            text="Prüfung läuft. Das Abrufen der Paketupdates kann einen Moment dauern.",
            background=self.colors["blue_bg"],
            foreground=self.colors["blue"],
        )
        threading.Thread(target=self.scan_worker, daemon=True).start()
        self.root.after(150, self.check_result)
        self.root.after(100, self.poll_activity)

    def open_update_manager(self) -> None:
        pamac = shutil.which("pamac")
        if pamac is None:
            messagebox.showerror(
                "Pamac nicht gefunden",
                "Pamac ist nicht installiert; Manjaro-/AUR-Updates können nicht gestartet werden.",
                parent=self.root,
            )
            return
        confirmed = messagebox.askyesno(
            "Systemupdates installieren",
            "Es werden verfügbare Manjaro-/Pacman- und AUR-Updates über Pamac "
            "sowie Flatpak- und Snap-Updates installiert. AUR-Pakete führen "
            "fremde Build-Skripte aus. Kernel- und Grafikupdates können einen "
            "Neustart erfordern.\n\nMöchtest du fortfahren?",
            icon="warning",
            parent=self.root,
        )
        if not confirmed:
            self.log_activity("Installation abgebrochen.")
            return
        if self.operation_mode is not None:
            return
        self.operation_mode = "install"
        self.scan_button.configure(state="disabled")
        self.install_button.configure(state="disabled")
        self.status.configure(text="Installiere Updates …", style="StatusIdle.TLabel")
        self.status_dot.configure(foreground=self.colors["blue"])
        self.progress.configure(mode="indeterminate")
        self.progress.start(100)
        self.update_banner.configure(
            text="Update-Vorgang läuft. Systemauthentifizierung kann über einen "
            "grafischen Dialog angefordert werden.",
            background=self.colors["blue_bg"],
            foreground=self.colors["blue"],
        )
        self.log_activity("Starte bestätigte Updates. Ausgabe wird hier angezeigt.")
        threading.Thread(target=self.install_worker, args=(pamac,), daemon=True).start()
        self.root.after(100, self.poll_activity)

    def scan_worker(self) -> None:
        try:
            self.results.put(scan_system(progress=self.activity_queue.put))
        except Exception as exc:
            self.results.put(exc)

    def poll_activity(self) -> None:
        while True:
            try:
                line = self.activity_queue.get_nowait()
            except queue.Empty:
                break
            self.log_activity(line)
            if line.startswith("--- ") and "starte Update" in line:
                self.progress.stop()
                self.progress.configure(mode="indeterminate")
                if self.operation_mode == "install":
                    self.progress.start(100)
            percentage = re.search(r"\b(\d{1,3})%", line)
            if percentage and self.operation_mode == "install":
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=100)
                self.progress["value"] = min(100, int(percentage.group(1)))

        if self.operation_mode == "install":
            try:
                outcomes = self.update_results.get_nowait()
            except queue.Empty:
                pass
            else:
                self.progress.stop()
                failures = [
                    f"{source}: {outcome}"
                    for source, outcome in outcomes
                    if isinstance(outcome, Exception) or outcome != 0
                ]
                if failures:
                    succeeded = [
                        source
                        for source, outcome in outcomes
                        if not isinstance(outcome, Exception) and outcome == 0
                    ]
                    self.operation_mode = None
                    self.scan_button.configure(state="normal")
                    self.install_button.configure(state="normal")
                    self.status.configure(text="Updates teilweise fehlgeschlagen", style="StatusWarning.TLabel")
                    self.status_dot.configure(foreground=self.colors["warning"])
                    for failure in failures:
                        self.log_activity(f"FEHLER: {failure}")
                    completed = (
                        f"Erfolgreich: {', '.join(succeeded)}. " if succeeded else ""
                    )
                    self.update_banner.configure(
                        text=f"{completed}Fehlgeschlagen: {'; '.join(failures)}",
                        background=self.colors["warning_bg"],
                        foreground=self.colors["warning"],
                    )
                    return
                self.progress.configure(mode="determinate", maximum=100)
                self.progress["value"] = 100
                self.log_activity("Alle verfügbaren Update-Vorgänge wurden abgeschlossen.")
                self._preserve_console = True
                self.operation_mode = None
                self.start_scan()
                return

        if self.operation_mode is not None:
            self.root.after(100, self.poll_activity)

    def install_worker(self, pamac: str) -> None:
        jobs = build_update_jobs(pamac)
        outcomes: list[tuple[str, int | Exception]] = []
        for source, arguments in jobs:
            self.activity_queue.put(f"--- {source}: starte Update ---")
            try:
                code = self.run_streaming_command(arguments)
                outcomes.append((source, code))
                if code == 0:
                    self.activity_queue.put(f"--- {source}: abgeschlossen ---")
                else:
                    self.activity_queue.put(f"--- {source}: Fehlercode {code} ---")
            except (OSError, CheckError) as exc:
                outcomes.append((source, exc))
                self.activity_queue.put(f"--- {source}: FEHLER: {exc} ---")
        self.update_results.put(outcomes)

    def run_streaming_command(self, arguments: list[str]) -> int:
        try:
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise CheckError(f"{arguments[0]} konnte nicht gestartet werden: {exc}") from exc
        if process.stdout is None:
            process.terminate()
            raise CheckError("Die Update-Ausgabe konnte nicht gelesen werden.")

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        stream = process.stdout
        while True:
            readable, _, _ = select.select([stream], [], [], 0.25)
            if readable:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                pending += decoder.decode(chunk)
                lines = re.split(r"[\r\n]+", pending)
                pending = lines.pop()
                for line in lines:
                    if line.strip():
                        self.activity_queue.put(ANSI_ESCAPE.sub("", line).strip())
            elif process.poll() is not None:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                pending += decoder.decode(chunk)
                lines = re.split(r"[\r\n]+", pending)
                pending = lines.pop()
                for line in lines:
                    if line.strip():
                        self.activity_queue.put(ANSI_ESCAPE.sub("", line).strip())
        pending += decoder.decode(b"", final=True)
        if pending.strip():
            self.activity_queue.put(ANSI_ESCAPE.sub("", pending).strip())
        return process.wait()

    def clear_console(self) -> None:
        if getattr(self, "_preserve_console", False):
            self._preserve_console = False
            return
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")
        self.console_line_count = 0

    def log_activity(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.console.configure(state="normal")
        self.console.insert("end", f"[{timestamp}] {text}\n")
        self.console_line_count += 1
        if self.console_line_count > 500:
            self.console.delete("1.0", "101.0")
            self.console_line_count = 400
        self.console.see("end")
        self.console.configure(state="disabled")

    def check_result(self) -> None:
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            self.root.after(150, self.check_result)
            return
        self.scan_button.configure(state="normal")
        self.install_button.configure(state="normal")
        self.operation_mode = None
        self.progress.stop()
        if isinstance(result, Exception):
            self.log_activity(f"FEHLER: Prüfung fehlgeschlagen: {result}")
            self.status.configure(text="Prüfung fehlgeschlagen", style="StatusWarning.TLabel")
            self.status_dot.configure(foreground=self.colors["warning"])
            self.update_banner.configure(
                text=f"Die Prüfung konnte nicht abgeschlossen werden.\n{result}",
                background=self.colors["warning_bg"],
                foreground=self.colors["warning"],
            )
            return
        self.log_activity("Hardware- und Paketquellenprüfung abgeschlossen.")
        status_style = "StatusWarning.TLabel" if result.errors else "StatusGood.TLabel"
        self.status.configure(
            text=f"Zuletzt geprüft · {datetime.now().strftime('%H:%M')}",
            style=status_style,
        )
        self.status_dot.configure(
            foreground=self.colors["warning"] if result.errors else self.colors["accent"]
        )
        self.render_result(result)

    @staticmethod
    def replace_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def render_result(self, result: ScanResult) -> None:
        related = [item for item in result.updates if update_is_driver_related(item)]
        update_error = next(
            (error for error in result.errors if error.startswith("Updates ")),
            None,
        )
        self.summary_values["pci"].configure(text=str(len(result.pci_devices)))
        self.summary_values["usb"].configure(text=str(len(result.usb_devices)))
        self.summary_values["updates"].configure(
            text=str(len(result.updates)) if not update_error else "!"
        )
        self.summary_notes["updates"].configure(
            text=(
                f"{len(related)} wahrscheinlich treiberrelevant"
                if not update_error
                else "Status konnte nicht ermittelt werden"
            )
        )
        self.kernel_label.configure(text=f"Laufender Kernel  ·  {result.kernel}")

        if result.updates:
            update_lines = "\n".join(f"• {item}" for item in result.updates)
            message = (
                f"{len(result.updates)} Paketaktualisierung(en) verfügbar; "
                f"{len(related)} davon wahrscheinlich treiberrelevant.\n\n{update_lines}"
            )
            self.update_banner.configure(
                text=message,
                background=self.colors["warning_bg"],
                foreground=self.colors["warning"],
            )
        elif update_error:
            self.update_banner.configure(
                text=f"Update-Status ist unvollständig.\n{update_error}",
                background=self.colors["warning_bg"],
                foreground=self.colors["warning"],
            )
        else:
            self.update_banner.configure(
                text="Dein System ist laut aktueller Paketdatenbank auf dem neuesten Stand. "
                "Keine Paketupdates verfügbar.",
                background=self.colors["good_bg"],
                foreground=self.colors["good"],
            )

        for item in self.device_table.get_children():
            self.device_table.delete(item)
        for device in result.pci_devices:
            driver = (
                f"Aktiv · {device.driver}"
                if device.driver
                else f"Modul verfügbar · {device.modules}"
                if device.modules
                else "Kein Treiber zugeordnet"
            )
            tags = ("unbound",) if not device.driver else ()
            self.device_table.insert(
                "", "end", values=("PCI", device.name, driver), tags=tags
            )
        for device in result.usb_devices:
            self.device_table.insert(
                "", "end", values=("USB", device, "Angeschlossen · Treiberstatus nicht geprüft")
            )
        self.replace_text(
            self.profile_text,
            result.mhwd_profiles or "Keine Manjaro-Treiberprofile gemeldet.",
        )
        self.replace_text(
            self.errors_text,
            "\n".join(f"• {error}" for error in result.errors)
            if result.errors
            else "Keine Fehler. Alle verfügbaren Prüfungen wurden erfolgreich abgeschlossen.",
        )


def main() -> None:
    root = tk.Tk()
    DriverCheckerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
