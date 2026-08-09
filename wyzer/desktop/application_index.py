"""Cached discovery of launchable Windows applications and game libraries."""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class IndexedApplication:
    name: str
    target: str
    source: str


class WindowsApplicationIndex:
    """Indexes launcher metadata and shortcuts without crawling entire disks."""

    def __init__(
        self,
        roots: list[Path] | None = None,
        environ: Mapping[str, str] | None = None,
        start_apps_loader: Callable[[], list[dict[str, str]]] | None = None,
        aliases: Mapping[str, str] | None = None,
    ) -> None:
        self._roots = roots
        self._environ = os.environ if environ is None else environ
        self._start_apps_loader = start_apps_loader or self._load_start_apps
        self._aliases = {
            "crumb": "chrome",
            "google crumb": "google chrome",
            **{_normalize(key): value for key, value in (aliases or {}).items()},
        }
        self._items: list[IndexedApplication] | None = None

    def search(self, query: str, limit: int = 20) -> list[IndexedApplication]:
        normalized = _normalize(query)
        normalized = _normalize(self._aliases.get(normalized, normalized))
        best_by_name: dict[str, IndexedApplication] = {}
        for item in self.all():
            name = _normalize(item.name)
            current = best_by_name.get(name)
            if current is None or _source_priority(item.source) > _source_priority(current.source):
                best_by_name[name] = item
        scored = [(_score(normalized, name), item) for name, item in best_by_name.items()]
        return [
            item
            for score, item in sorted(
                scored,
                key=lambda row: (-row[0], row[1].name.casefold()),
            )
            if score >= 0.65
        ][:limit]

    def resolve(self, query: str) -> IndexedApplication | None:
        matches = self.search(query, 2)
        if not matches:
            return None
        wanted = _normalize(query)
        wanted = _normalize(self._aliases.get(wanted, wanted))
        top_name = _normalize(matches[0].name)
        same_name = [item for item in self.all() if _normalize(item.name) == top_name]
        best = max(same_name or [matches[0]], key=lambda item: _source_priority(item.source))
        score = _score(wanted, _normalize(best.name))
        different = next((item for item in matches[1:] if _normalize(item.name) != top_name), None)
        runner_up = _score(wanted, _normalize(different.name)) if different is not None else 0
        return best if score >= 0.72 and score - runner_up >= 0.08 else None

    def all(self) -> list[IndexedApplication]:
        if self._items is None:
            found: dict[tuple[str, str], IndexedApplication] = {}
            xbox_names = self._xbox_game_names()
            for item in [
                *self._start_menu(),
                *self._store_apps(xbox_names),
                *self._steam(),
                *self._epic(),
                *self._ea(),
                *self._registered_ea(),
                *self._battlenet(),
            ]:
                found.setdefault((_normalize(item.name), item.target.casefold()), item)
            self._items = sorted(found.values(), key=lambda item: item.name.casefold())
        return list(self._items)

    def refresh(self) -> list[IndexedApplication]:
        self._items = None
        return self.all()

    def games(self) -> list[IndexedApplication]:
        game_sources = {"steam", "epic", "xbox_game", "ea", "battlenet"}
        excluded = {
            "3dmark",
            "ea app",
            "minecraft launcher",
            "steamvr",
            "steamworks common redistributables",
            "wallpaper engine",
        }
        games: dict[str, IndexedApplication] = {}
        for item in self.all():
            name = _normalize(item.name)
            if item.source not in game_sources or name in excluded or name.endswith(" launcher"):
                continue
            current = games.get(name)
            if current is None or _source_priority(item.source) > _source_priority(current.source):
                games[name] = item
        return sorted(games.values(), key=lambda item: item.name.casefold())

    def _drive_roots(self) -> list[Path]:
        if self._roots is not None:
            return self._roots
        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
        return [Path(f"{chr(65 + bit)}:/") for bit in range(26) if mask & (1 << bit)]

    def _start_menu(self) -> list[IndexedApplication]:
        locations = []
        for variable, suffix in (
            ("APPDATA", "Microsoft/Windows/Start Menu/Programs"),
            ("PROGRAMDATA", "Microsoft/Windows/Start Menu/Programs"),
        ):
            base = self._environ.get(variable)
            if base:
                locations.append(Path(base, *suffix.split("/")))
        items: list[IndexedApplication] = []
        for location in locations:
            try:
                shortcuts = location.rglob("*.lnk")
                for shortcut in shortcuts:
                    items.append(IndexedApplication(shortcut.stem, str(shortcut), "start_menu"))
            except OSError:
                continue
        return items

    def _xbox_game_names(self) -> set[str]:
        names: set[str] = set()
        for root in self._drive_roots():
            games = root / "XboxGames"
            try:
                for directory in games.iterdir():
                    config = directory / "Content/MicrosoftGame.Config"
                    try:
                        document = ET.parse(config)
                    except (OSError, ET.ParseError):
                        continue
                    visuals = document.getroot().find("ShellVisuals")
                    display = visuals.get("DefaultDisplayName") if visuals is not None else None
                    if display and not display.startswith("ms-resource:"):
                        names.add(_normalize(display))
                    names.add(_normalize(directory.name))
            except OSError:
                continue
        return names

    def _store_apps(self, xbox_names: set[str]) -> list[IndexedApplication]:
        items: list[IndexedApplication] = []
        for app in self._start_apps_loader():
            name, app_id = app.get("Name"), app.get("AppID")
            if not name or not app_id:
                continue
            source = "xbox_game" if _normalize(name) in xbox_names else "microsoft_store"
            items.append(IndexedApplication(name, f"shell:AppsFolder\\{app_id}", source))
        return items

    @staticmethod
    def _load_start_apps() -> list[dict[str, str]]:
        command = "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            raw = json.loads(completed.stdout) if completed.returncode == 0 else []
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return []
        records = raw if isinstance(raw, list) else [raw]
        return [
            {"Name": str(item["Name"]), "AppID": str(item["AppID"])}
            for item in records
            if isinstance(item, dict) and item.get("Name") and item.get("AppID")
        ]

    def _ea(self) -> list[IndexedApplication]:
        items: list[IndexedApplication] = []
        relative_roots = (
            "EA Games",
            "Games/EA",
            "games/EA",
            "Program Files/EA Games",
            "Program Files (x86)/EA Games",
        )
        for drive in self._drive_roots():
            for relative in relative_roots:
                library = drive / relative
                try:
                    games = [path for path in library.iterdir() if path.is_dir()]
                except OSError:
                    continue
                for game in games:
                    executable = _select_game_executable(game)
                    if executable is not None:
                        items.append(IndexedApplication(game.name, str(executable), "ea"))
        return items

    def _battlenet(self) -> list[IndexedApplication]:
        app_data = self._environ.get("APPDATA")
        if not app_data:
            return []
        config = Path(app_data) / "Battle.net/Battle.net.config"
        try:
            games = json.loads(config.read_text(encoding="utf-8-sig")).get("Games", {})
        except (OSError, ValueError, AttributeError):
            return []
        names = {
            "fenris": "Diablo IV",
            "d3": "Diablo III",
            "osi": "Diablo II: Resurrected",
            "pro": "Overwatch 2",
            "s2": "StarCraft II",
            "hero": "Heroes of the Storm",
            "wow": "World of Warcraft",
            "wow_classic": "World of Warcraft Classic",
            "wtcg": "Hearthstone",
            "odin": "Call of Duty",
        }
        return [
            IndexedApplication(names[code], f"battlenet-product:{code}", "battlenet")
            for code, details in games.items()
            if code in names
            and code != "battle_net"
            and isinstance(details, dict)
            and str(details.get("Resumable", "true")).casefold() != "false"
        ]

    @staticmethod
    def _registered_ea() -> list[IndexedApplication]:
        try:
            import winreg
        except ImportError:
            return []
        items: list[IndexedApplication] = []
        locations = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        )
        for location in locations:
            try:
                root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, location)
            except OSError:
                continue
            with root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        child = winreg.OpenKey(root, winreg.EnumKey(root, index))
                        with child:
                            name = str(winreg.QueryValueEx(child, "DisplayName")[0])
                            publisher = _registry_value(winreg, child, "Publisher")
                            uninstall = _registry_value(winreg, child, "UninstallString")
                            icon = _registry_value(winreg, child, "DisplayIcon")
                    except OSError:
                        continue
                    is_ea = (
                        "electronic arts" in publisher.casefold()
                        or "eainstaller" in uninstall.casefold()
                    )
                    target = icon.strip().strip('"').rsplit(",", 1)[0].strip('"')
                    if is_ea and target.casefold().endswith(".exe") and Path(target).is_file():
                        items.append(IndexedApplication(name, target, "ea"))
        return items

    def _steam(self) -> list[IndexedApplication]:
        steam_roots: set[Path] = set()
        candidates: list[Path] = []
        for root in self._drive_roots():
            candidates.extend(
                root / relative
                for relative in (
                    "Program Files (x86)/Steam",
                    "Program Files/Steam",
                    "Steam",
                    "SteamLibrary",
                )
            )
        for candidate in candidates:
            if (candidate / "steamapps").is_dir():
                steam_roots.add(candidate)
            library_file = candidate / "steamapps/libraryfolders.vdf"
            try:
                text = library_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for raw in re.findall(r'"path"\s+"([^"]+)"', text):
                steam_roots.add(Path(raw.replace("\\\\", "\\")))
        items: list[IndexedApplication] = []
        for root in steam_roots:
            try:
                manifests = (root / "steamapps").glob("appmanifest_*.acf")
                for manifest in manifests:
                    text = manifest.read_text(encoding="utf-8", errors="ignore")
                    name = _vdf_value(text, "name")
                    app_id = _vdf_value(text, "appid")
                    if name and app_id:
                        items.append(
                            IndexedApplication(name, f"steam://rungameid/{app_id}", "steam")
                        )
            except OSError:
                continue
        return items

    def _epic(self) -> list[IndexedApplication]:
        manifests: list[Path] = []
        program_data = self._environ.get("PROGRAMDATA")
        if program_data:
            manifests.append(Path(program_data) / "Epic/UnrealEngineLauncher/LauncherInstalled.dat")
        for root in self._drive_roots():
            manifests.append(root / "ProgramData/Epic/UnrealEngineLauncher/LauncherInstalled.dat")
        items: list[IndexedApplication] = []
        for manifest in dict.fromkeys(manifests):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            for app in data.get("InstallationList", []):
                if not isinstance(app, dict):
                    continue
                install_location = app.get("InstallLocation")
                install_name = (
                    Path(install_location).name
                    if isinstance(install_location, str) and install_location
                    else None
                )
                app_name = app.get("AppName") or app.get("ArtifactId")
                # Epic's installed manifest frequently omits DisplayName and uses an
                # internal codename (Rocket League is "Sugar"). The install folder
                # remains a useful human-facing alias in that case.
                name = app.get("DisplayName") or install_name or app_name
                namespace = app.get("NamespaceId")
                catalog = app.get("CatalogItemId") or app.get("ItemId")
                if all(isinstance(value, str) and value for value in (name, app_name)):
                    identifier = ":".join(
                        value for value in (namespace, catalog, app_name) if isinstance(value, str)
                    )
                    target = (
                        f"com.epicgames.launcher://apps/{quote(identifier, safe='')}?action=launch"
                    )
                    items.append(IndexedApplication(str(name), target, "epic"))
        return items


def _vdf_value(text: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s+"([^"]+)"', text, re.IGNORECASE)
    return match.group(1) if match else None


def _select_game_executable(game: Path) -> Path | None:
    rejected = ("unins", "uninstall", "crash", "report", "setup", "update", "installer")
    try:
        executables = [
            path
            for path in game.glob("**/*.exe")
            if len(path.relative_to(game).parts) <= 4
            and not any(word in path.name.casefold() for word in rejected)
        ]
    except OSError:
        return None
    normalized = _normalize(game.name)
    executables.sort(
        key=lambda path: (
            _score(normalized, _normalize(path.stem)),
            -len(path.relative_to(game).parts),
        ),
        reverse=True,
    )
    return executables[0] if executables else None


def _registry_value(module: object, key: object, name: str) -> str:
    try:
        value = module.QueryValueEx(key, name)[0]  # type: ignore[attr-defined]
    except OSError:
        return ""
    return str(value or "")


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _score(query: str, name: str) -> float:
    if query == name:
        return 1.0
    if name.startswith(query) or query.startswith(name):
        return 0.9
    if query in name:
        return 0.82
    return SequenceMatcher(None, query, name).ratio()


def _source_priority(source: str) -> int:
    return {
        "battlenet": 7,
        "ea": 7,
        "epic": 6,
        "steam": 6,
        "xbox_game": 6,
        "start_menu": 3,
        "microsoft_store": 2,
    }.get(source, 1)
