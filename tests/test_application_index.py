import json
from pathlib import Path
from typing import cast

from pytest import MonkeyPatch

from wyzer.desktop.application_index import IndexedApplication, WindowsApplicationIndex
from wyzer.desktop.windows_backend import CtypesWindowsBackend


def test_indexes_steam_and_epic_libraries_on_every_supplied_drive(tmp_path: Path) -> None:
    first = tmp_path / "C"
    second = tmp_path / "D"
    steamapps = second / "SteamLibrary/steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "appmanifest_252950.acf").write_text(
        '"AppState" { "appid" "252950" "name" "Rocket League Steam" }',
        encoding="utf-8",
    )
    epic_file = first / "ProgramData/Epic/UnrealEngineLauncher/LauncherInstalled.dat"
    epic_file.parent.mkdir(parents=True)
    epic_file.write_text(
        json.dumps(
            {
                "InstallationList": [
                    {
                        "AppName": "Sugar",
                        "NamespaceId": "rocket-namespace",
                        "ItemId": "rocket-catalog",
                        "InstallLocation": str(second / "Games/RocketLeague"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    index = WindowsApplicationIndex(
        roots=[first, second],
        environ={"PROGRAMDATA": str(first / "ProgramData")},
        start_apps_loader=lambda: [],
    )

    names = {item.name for item in index.all()}
    assert {"RocketLeague", "Rocket League Steam"} <= names
    assert {item.source for item in index.search("rocket league")} == {"epic", "steam"}
    rocket = index.resolve("rocketleague")
    assert rocket is not None
    assert rocket.source == "epic"
    assert rocket.target.startswith("com.epicgames.launcher://apps/")


def test_indexes_start_menu_shortcuts(tmp_path: Path) -> None:
    shortcut = tmp_path / "AppData/Microsoft/Windows/Start Menu/Programs/My Editor.lnk"
    shortcut.parent.mkdir(parents=True)
    shortcut.touch()
    index = WindowsApplicationIndex(
        roots=[], environ={"APPDATA": str(tmp_path / "AppData")}, start_apps_loader=lambda: []
    )
    match = index.resolve("my editor")
    assert match is not None
    assert match.target == str(shortcut)


def test_search_deduplicates_names_and_filters_weak_matches() -> None:
    index = WindowsApplicationIndex(roots=[], environ={}, start_apps_loader=lambda: [])
    index._items = [
        IndexedApplication("Calculator", "store:calculator", "microsoft_store"),
        IndexedApplication("Calculator", "calculator.lnk", "start_menu"),
        IndexedApplication("Calendar", "calendar.lnk", "start_menu"),
        IndexedApplication("Narrator", "narrator.lnk", "start_menu"),
    ]

    matches = index.search("Calculator")

    assert [(item.name, item.source) for item in matches] == [("Calculator", "start_menu")]


def test_known_windows_alias_wins_over_fuzzy_index_match(monkeypatch: MonkeyPatch) -> None:
    class FuzzyIndex:
        @staticmethod
        def resolve(query: str) -> object:
            raise AssertionError(f"the index must not resolve the known alias {query}")

    class Process:
        pid = 123

    backend = CtypesWindowsBackend.__new__(CtypesWindowsBackend)
    backend._applications = cast(WindowsApplicationIndex, FuzzyIndex())
    monkeypatch.setattr(
        CtypesWindowsBackend,
        "_application_command",
        classmethod(lambda cls, requested: (["notepad.exe"], "notepad.exe")),
    )
    monkeypatch.setattr(
        CtypesWindowsBackend,
        "_spawn_silently",
        staticmethod(lambda command: Process()),
    )

    process_id, executable = backend.launch_application("Notepad")

    assert process_id == 123
    assert executable == "notepad.exe"


def test_indexes_registered_xbox_game_installed_on_secondary_drive(tmp_path: Path) -> None:
    second = tmp_path / "D"
    config = second / "XboxGames/Forza Horizon 6/Content/MicrosoftGame.Config"
    config.parent.mkdir(parents=True)
    config.write_text(
        '<Game><ShellVisuals DefaultDisplayName="Forza Horizon 6" /></Game>',
        encoding="utf-8",
    )
    index = WindowsApplicationIndex(
        roots=[tmp_path / "C", second],
        environ={},
        start_apps_loader=lambda: [
            {
                "Name": "Forza Horizon 6",
                "AppID": "Microsoft.ForteBaseGame_8wekyb3d8bbwe!Forzahorizon6",
            }
        ],
    )

    forza = index.resolve("forza horizon 6")
    assert forza is not None
    assert forza.source == "xbox_game"
    assert forza.target == ("shell:AppsFolder\\Microsoft.ForteBaseGame_8wekyb3d8bbwe!Forzahorizon6")


def test_indexes_ea_and_battlenet_games(tmp_path: Path) -> None:
    second = tmp_path / "D"
    ea_executable = second / "EA Games/Mass Effect/MassEffect.exe"
    ea_executable.parent.mkdir(parents=True)
    ea_executable.touch()
    app_data = tmp_path / "AppData"
    battle_config = app_data / "Battle.net/Battle.net.config"
    battle_config.parent.mkdir(parents=True)
    battle_config.write_text(
        json.dumps({"Games": {"battle_net": {}, "fenris": {"Resumable": "true"}}}),
        encoding="utf-8",
    )
    index = WindowsApplicationIndex(
        roots=[tmp_path / "C", second],
        environ={"APPDATA": str(app_data)},
        start_apps_loader=lambda: [],
    )

    ea = index.resolve("mass effect")
    battle = index.resolve("diablo 4")
    assert ea is not None and ea.source == "ea" and ea.target == str(ea_executable)
    assert battle is not None and battle.source == "battlenet"
    assert battle.target == "battlenet-product:fenris"


def test_exact_strong_fuzzy_and_configured_speech_alias_resolution() -> None:
    index = WindowsApplicationIndex(
        roots=[],
        environ={},
        start_apps_loader=lambda: [
            {"Name": "Google Chrome", "AppID": "chrome.app"},
            {"Name": "Calculator", "AppID": "calculator.app"},
        ],
    )
    assert index.resolve("Google Chrome") is not None
    assert index.resolve("Googel Chrome") is not None
    crumb = index.resolve("crumb")
    assert crumb is not None and crumb.name == "Google Chrome"


def test_ambiguous_application_match_does_not_resolve_wrong_target() -> None:
    index = WindowsApplicationIndex(
        roots=[],
        environ={},
        start_apps_loader=lambda: [
            {"Name": "Photos", "AppID": "photos.app"},
            {"Name": "PhotoScape", "AppID": "photoscape.app"},
        ],
    )
    assert index.resolve("photo") is None
    assert [candidate.name for candidate in index.search("photo", 2)] == [
        "Photos",
        "PhotoScape",
    ]
