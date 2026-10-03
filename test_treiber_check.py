import unittest
import threading
import time
from unittest.mock import patch

from treiber_check import (
    build_update_jobs,
    parse_pci_devices,
    parse_updates,
    parse_usb_devices,
    scan_system,
    update_is_driver_related,
)


class ParserTests(unittest.TestCase):
    def test_parse_pci_devices_includes_bound_driver_and_possible_modules(self):
        devices = parse_pci_devices(
            "00:02.0 VGA compatible controller [0300]: Intel Graphics [8086:1234]\n"
            "\tKernel driver in use: i915\n"
            "\tKernel modules: i915\n"
            "00:08.0 Generic system peripheral [0880]: Device [1234:5678]\n"
        )
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0].driver, "i915")
        self.assertEqual(devices[0].modules, "i915")
        self.assertIsNone(devices[1].driver)

    def test_parse_usb_devices_omits_root_hubs(self):
        devices = parse_usb_devices(
            "Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub\n"
            "Bus 001 Device 002: ID 8087:0a2b Intel Corp. Bluetooth interface\n"
        )
        self.assertEqual(devices, ["Intel Corp. Bluetooth interface"])

    def test_parse_updates_and_recognize_driver_related_packages(self):
        updates = parse_updates("linux618 6.18.1 -> 6.18.2\nfirefox 1.0 -> 1.1\n")
        self.assertEqual(len(updates), 2)
        self.assertTrue(update_is_driver_related(updates[0]))
        self.assertFalse(update_is_driver_related(updates[1]))

    def test_parse_updates_ignores_manager_empty_messages_and_snap_header(self):
        self.assertEqual(parse_updates("All snaps up to date.\n"), [])
        self.assertEqual(
            parse_updates(
                "Name Version Rev Size Publisher Notes\n"
                "browser 1.2 10 20MB publisher -\n"
            ),
            ["browser 1.2 10 20MB publisher -"],
        )

    def test_driver_package_detection_handles_source_prefix(self):
        self.assertTrue(update_is_driver_related("[Manjaro/Pacman] linux618 6.18.1 -> 6.18.2"))
        self.assertTrue(update_is_driver_related("[Snap] nvidia-driver 1 -> 2"))
        self.assertFalse(update_is_driver_related("[Flatpak] org.example.App 1 -> 2"))

    def test_update_jobs_enable_graphical_auth_for_flatpak_and_snap(self):
        with patch(
            "treiber_check.shutil.which",
            side_effect=lambda name: {
                "flatpak": "/usr/bin/flatpak",
                "snap": "/usr/bin/snap",
                "pkexec": "/usr/bin/pkexec",
            }.get(name),
        ):
            jobs = build_update_jobs("/usr/bin/pamac")

        self.assertEqual(
            jobs,
            [
                (
                    "Manjaro-/Pacman- und AUR-Pakete (Pamac)",
                    ["/usr/bin/pamac", "upgrade", "--aur", "--no-confirm"],
                ),
                ("Flatpak", ["/usr/bin/flatpak", "update", "--assumeyes"]),
                ("Snap", ["/usr/bin/pkexec", "/usr/bin/snap", "refresh"]),
            ],
        )

    def test_scan_runs_independent_checks_in_parallel(self):
        active = 0
        peak_active = 0
        lock = threading.Lock()
        output = {
            "uname": "6.18-test",
            "lspci": "",
            "lsusb": "",
            "mhwd": "",
            "pamac": "",
            "yay": "",
            "flatpak": "",
            "snap": "All snaps up to date.",
        }

        def fake_run_command(arguments, **_kwargs):
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                time.sleep(0.04)
                return output[arguments[0].rsplit("/", 1)[-1]]
            finally:
                with lock:
                    active -= 1

        def fake_which(name):
            return f"/usr/bin/{name}" if name in {"yay", "flatpak", "snap"} else None

        with (
            patch("treiber_check.platform.system", return_value="Linux"),
            patch("treiber_check.shutil.which", side_effect=fake_which),
            patch("treiber_check.run_command", side_effect=fake_run_command),
        ):
            result = scan_system()

        self.assertGreater(peak_active, 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.kernel, "6.18-test")


if __name__ == "__main__":
    unittest.main()
