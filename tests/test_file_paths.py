from wyzer.files.paths import dominant_location


def test_dominant_location_prefers_project_cluster_over_sibling_archive() -> None:
    paths = [
        r"C:\Users\me\Documents\Project\src\main.cpp",
        r"C:\Users\me\Documents\Project\docs\README.md",
        r"C:\Users\me\Documents\Project\config.json",
        r"C:\Users\me\Documents\Project.zip",
    ]
    assert dominant_location(paths) == r"C:\Users\me\Documents\Project"


def test_dominant_location_prefers_an_exact_named_ancestor() -> None:
    paths = [
        r"D:\downloads\prius analyzer\PriusBatteryTester\AI-Assistant-v2\wyzer\one.py",
        r"D:\downloads\prius analyzer\PriusBatteryTester\prius-tester\main.cpp",
    ]
    assert dominant_location(paths, "prius analyzer") == r"D:\downloads\prius analyzer"


def test_dominant_location_prefers_named_project_over_incidental_text_cluster() -> None:
    paths = [
        r"C:\Users\me\Documents\PriusSolarController\platformio.ini",
        r"C:\Users\me\Documents\PriusSolarController\src\main.cpp",
        r"C:\tools\plugins\skills\one.md",
        r"C:\tools\plugins\skills\two.md",
        r"C:\tools\plugins\skills\three.md",
    ]

    assert dominant_location(paths, "solar project") == (
        r"C:\Users\me\Documents\PriusSolarController"
    )
