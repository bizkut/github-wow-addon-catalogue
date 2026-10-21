# ruff: noqa: PT009
import datetime as dt
import io
import unittest
from zipfile import ZipFile

from cataloguer import (
    Project,
    ReleaseAsset,
    ReleaseInfo,
    RepositoryInfo,
    _asset_pattern_matches,
    analyse_addon_zip,
    expand_repository_releases,
)


def addon_zip(*files: tuple[str, str]) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for name, contents in files:
            archive.writestr(name, contents)
    return output.getvalue()


def release(tag: str, *assets: ReleaseAsset) -> ReleaseInfo:
    return ReleaseInfo(
        tag_name=tag,
        published_at=dt.datetime(2026, 10, 20, tzinfo=dt.UTC),
        prerelease=False,
        draft=False,
        assets=assets,
    )


class CatalogueRowsTest(unittest.TestCase):
    def setUp(self):
        self.repo = RepositoryInfo(
            id="1234",
            name="Pack",
            full_name="owner/pack",
            url="https://github.com/owner/pack",
            description="A pack",
        )

    def test_csv_old_and_extended_rows_round_trip(self):
        old = {
            "id": "1",
            "name": "Old",
            "full_name": "owner/old",
            "url": "https://github.com/owner/old",
            "description": "Old row",
            "last_updated": "2026-10-20T00:00:00+00:00",
            "flavors": "forever",
            "curse_id": "",
            "wago_id": "",
            "wowi_id": "",
            "has_release_json": "False",
            "last_seen": "2026-10-20T00:00:00+00:00",
        }
        parsed = Project.from_csv_row(old)
        self.assertIsNone(parsed.release_tag_prefix)
        self.assertIsNone(parsed.asset_name_pattern)
        self.assertEqual(parsed.folders, ())

        extended = old | {
            "id": "2",
            "release_tag_prefix": "quests-audio/",
            "asset_name_pattern": "SpokenQuestsAudioAll-*.zip",
            "folders": "SpokenQuestsAudioAll,SpokenShared",
        }
        parsed = Project.from_csv_row(extended)
        self.assertEqual(parsed.release_tag_prefix, "quests-audio/")
        self.assertEqual(parsed.asset_name_pattern, "SpokenQuestsAudioAll-*.zip")
        self.assertEqual(parsed.folders, ("SpokenQuestsAudioAll", "SpokenShared"))
        row = parsed.to_csv_row()
        self.assertEqual(row["folders"], "SpokenQuestsAudioAll,SpokenShared")

    def test_archive_admission_requires_top_level_toc_flavour_and_folder(self):
        valid = analyse_addon_zip(
            addon_zip(("Spoken/Spoken.toc", "## Interface: 16001\n## X-Wago-ID: abc\n")),
            "Spoken-1.zip",
        )
        self.assertEqual(valid.folders, ("Spoken",))
        self.assertEqual({str(f) for f in valid.flavors}, {"forever"})
        self.assertEqual(valid.wago_id, "abc")

        self.assertIsNone(
            analyse_addon_zip(addon_zip(("nested/x/X.toc", "## Interface: 16001")), "x.zip")
        )
        self.assertIsNone(
            analyse_addon_zip(addon_zip(("X/X.toc", "## Title: no interface")), "x.zip")
        )
        self.assertIsNone(analyse_addon_zip(addon_zip(("readme.txt", "not an addon")), "x.zip"))

    def test_generic_tag_streams_create_rows_and_non_addon_streams_drop(self):
        spoken = ReleaseAsset(
            "Spoken-3.5.1.zip",
            12 * 1024 * 1024,
            "https://example/spoken.zip",
            analyse_addon_zip(
                addon_zip(("Spoken/Spoken.toc", "## Interface: 16001")), "Spoken-3.5.1.zip"
            ),
        )
        future = ReleaseAsset(
            "FuturePack-1.zip",
            452 * 1024 * 1024,
            "https://example/future.zip",
            analyse_addon_zip(
                addon_zip(("FuturePack/FuturePack.toc", "## Interface: 16001")), "FuturePack-1.zip"
            ),
        )
        installer = ReleaseAsset("Manager.exe", 1, "https://example/manager.exe", None)
        rows = expand_repository_releases(
            self.repo,
            [
                release("spoken/v3.5.1", spoken),
                release("future-audio/v1.0.0", future),
                release("manager/v1.0.0", installer),
            ],
            now=dt.datetime(2026, 10, 21, tzinfo=dt.UTC),
        )
        self.assertEqual({r.release_tag_prefix for r in rows}, {"spoken/", "future-audio/"})
        future_row = next(r for r in rows if r.release_tag_prefix == "future-audio/")
        self.assertEqual(future_row.asset_name_pattern, "FuturePack-1.zip")
        self.assertEqual(future_row.folders, ("FuturePack",))
        self.assertIn("452 MiB", future_row.description)
        self.assertEqual(future_row.name, "Future Audio (All)")

    def test_multi_zip_distinct_addons_split_same_folder_variants_do_not(self):
        main_info = analyse_addon_zip(
            addon_zip(("LoreForever/LoreForever.toc", "## Interface: 16001")), "LoreForever.zip"
        )
        voice_info = analyse_addon_zip(
            addon_zip(("LoreVoice/LoreVoice.toc", "## Interface: 16001")), "LoreVoice.zip"
        )
        main = ReleaseAsset("LoreForever.zip", 10, "https://example/main", main_info)
        main_repack = ReleaseAsset("LoreForever-nolib.zip", 9, "https://example/nolib", main_info)
        voice = ReleaseAsset("LoreForever_Voice.zip", 20, "https://example/voice", voice_info)
        rows = expand_repository_releases(
            self.repo,
            [release("v1.0.0", main, main_repack, voice)],
            now=dt.datetime(2026, 10, 21, tzinfo=dt.UTC),
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.folders for r in rows}, {("LoreForever",), ("LoreVoice",)})
        patterns = {r.asset_name_pattern for r in rows}
        self.assertIn("LoreForever_Voice.zip", patterns)
        self.assertNotIn("LoreForever-nolib.zip", patterns)
        self.assertIn("Lore Forever", {row.name for row in rows})

    def test_ids_and_patterns_stay_stable_across_release_versions(self):
        main_info = analyse_addon_zip(
            addon_zip(("Polestar/Polestar.toc", "## Interface: 16001")), "Polestar.zip"
        )
        recorder_info = analyse_addon_zip(
            addon_zip(("Polestar_Recorder/Polestar_Recorder.toc", "## Interface: 16001")),
            "Polestar_Recorder.zip",
        )

        def rows_for(version: str):
            main = ReleaseAsset(
                f"Polestar-{version}.zip", 10, "https://example/main", main_info
            )
            recorder = ReleaseAsset(
                f"Polestar_Recorder-{version}.zip",
                10,
                "https://example/recorder",
                recorder_info,
            )
            return expand_repository_releases(
                self.repo,
                [release(f"v{version}", main, recorder)],
                now=dt.datetime(2026, 10, 21, tzinfo=dt.UTC),
            )

        old = next(row for row in rows_for("0.2.6") if row.folders == ("Polestar",))
        new = next(row for row in rows_for("0.2.7") if row.folders == ("Polestar",))
        self.assertEqual(old.id, "1234:polestar-zip")
        self.assertEqual(old.id, new.id)
        self.assertEqual(old.asset_name_pattern, "Polestar-*.zip")
        self.assertEqual(old.asset_name_pattern, new.asset_name_pattern)
        self.assertTrue(_asset_pattern_matches(old.asset_name_pattern, "Polestar-0.2.7.zip"))


if __name__ == "__main__":
    unittest.main()
