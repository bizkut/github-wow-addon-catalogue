# ruff: noqa: PT009, PT027
import datetime as dt
import io
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from zipfile import ZipFile

from cataloguer import (
    Project,
    ReleaseAsset,
    ReleaseInfo,
    RepositoryInfo,
    _asset_pattern_matches,
    _graphql_repositories,
    _projects_from_graphql,
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
        self.assertIn(" - 452 MiB", future_row.description)
        self.assertTrue(future_row.description.isascii())
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


class ReleaseDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def discover(self, *, fallback: bool = False):
        spoken = {
            "databaseId": 1360371543,
            "name": "spoken-wow",
            "nameWithOwner": "rusty-key/spoken-wow",
            "url": "https://github.com/rusty-key/spoken-wow",
            "description": "Spoken",
        }
        contents = addon_zip(
            ("SpokenZonesAudio_ptBR/SpokenZonesAudio_ptBR.toc", "## Interface: 16001")
        )
        asset = ReleaseAsset(
            "SpokenZonesAudio_ptBR-2.0.0.zip",
            len(contents),
            "https://example/places.zip",
        )

        def node(tag: str, assets=()):
            return {
                "tagName": tag,
                "publishedAt": "2026-09-26T17:48:33Z",
                "isPrerelease": False,
                "isDraft": False,
                "releaseAssets": {"nodes": list(assets)},
            }

        def connection(nodes, cursor, more):
            return {
                "nodes": nodes,
                "pageInfo": {"endCursor": cursor, "hasNextPage": more},
            }

        calls = []
        page_size = 20 if fallback else 100

        @asynccontextmanager
        async def post(url, *, json):
            calls.append(json)
            query = json["query"]
            if fallback and "first:100," in query:
                payload = {"errors": [{"message": "Query too expensive"}]}
            else:
                cursor = json["variables"].get("cursor")
                if cursor is None:
                    releases = connection(
                        [node(f"spoken/v{i}") for i in range(page_size, 0, -1)], "page-1", True
                    )
                elif cursor == "page-1":
                    releases = connection([node("manager/v1.0.0")], "page-2", True)
                else:
                    self.assertEqual(cursor, "page-2")
                    releases = connection(
                        [
                            node(
                                "zones-audio-ptBR/v2.0.0",
                                [
                                    {
                                        "name": asset.name,
                                        "size": asset.size,
                                        "downloadUrl": asset.download_url,
                                    }
                                ],
                            )
                        ],
                        "page-3",
                        False,
                    )
                payload = {"data": {"r0": spoken | {"releases": releases}}}
            response = AsyncMock(status=200)
            response.json.return_value = payload
            yield response

        client = AsyncMock()
        client.post = post
        repositories = await _graphql_repositories(client, ["rusty-key/spoken-wow"])
        self.assertEqual(len(repositories), 1)
        self.assertEqual(len(repositories[0]["releases"]["nodes"]), page_size + 2)
        self.assertTrue(all("pageInfo" in call["query"] for call in calls))
        self.assertEqual(
            [
                call["variables"].get("cursor")
                for call in calls
                if not fallback or "first:20," in call["query"]
            ],
            [None, "page-1", "page-2"],
        )
        self.assertEqual(
            [100 if "releases(first:100," in call["query"] else 20 for call in calls],
            [100, 20, 20, 20] if fallback else [100, 100, 100],
        )

        @asynccontextmanager
        async def get(url, *, headers, expire_after):
            self.assertEqual(url, asset.download_url)
            self.assertEqual(expire_after, 0)
            start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            response = AsyncMock(status=206)
            response.read.return_value = contents[start : end + 1]
            yield response

        projects = await _projects_from_graphql(client, get, repositories)
        self.assertEqual(len(projects), 1)
        project = next(iter(projects))
        self.assertEqual(project.id, "1360371543:zones-audio-ptbr")
        self.assertEqual(project.release_tag_prefix, "zones-audio-ptBR/")
        self.assertEqual(project.asset_name_pattern, "SpokenZonesAudio_ptBR-*.zip")
        self.assertEqual(project.folders, ("SpokenZonesAudio_ptBR",))
        self.assertEqual({str(flavor) for flavor in project.flavors}, {"forever"})

    async def test_valid_stream_beyond_first_hundred_releases_is_discovered(self):
        await self.discover()

    async def test_reduced_page_size_fallback_also_paginates(self):
        await self.discover(fallback=True)

    async def test_batched_repositories_keep_independent_release_cursors(self):
        first = {
            "nameWithOwner": "owner/first",
            "releases": {
                "nodes": [{"tagName": "first/v2"}],
                "pageInfo": {"endCursor": "first-page", "hasNextPage": True},
            },
        }
        second = {
            "nameWithOwner": "owner/second",
            "releases": {
                "nodes": [],
                "pageInfo": {"endCursor": None, "hasNextPage": False},
            },
        }
        last = {
            "nameWithOwner": "owner/first",
            "releases": {
                "nodes": [{"tagName": "other/v1"}],
                "pageInfo": {"endCursor": "last-page", "hasNextPage": False},
            },
        }
        calls = []

        @asynccontextmanager
        async def post(url, *, json):
            calls.append(json["variables"])
            response = AsyncMock(status=200)
            response.json.return_value = {
                "data": {"r0": first, "r1": second} if len(calls) == 1 else {"r0": last}
            }
            yield response

        client = AsyncMock()
        client.post = post
        rows = await _graphql_repositories(client, ["owner/first", "owner/second"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], {"owner0": "owner", "name0": "first", "cursor": "first-page"})
        self.assertEqual(
            [node["tagName"] for node in rows[0]["releases"]["nodes"]],
            ["first/v2", "other/v1"],
        )
        self.assertEqual(rows[1]["releases"]["nodes"], [])

    async def test_failed_later_page_does_not_publish_a_truncated_history(self):
        @asynccontextmanager
        async def post(url, *, json):
            response = AsyncMock(status=200)
            response.json.return_value = (
                {"errors": [{"message": "Unavailable"}]}
                if json["variables"].get("cursor")
                else {
                    "data": {
                        "r0": {
                            "nameWithOwner": "owner/pack",
                            "releases": {
                                "nodes": [],
                                "pageInfo": {"endCursor": "page-1", "hasNextPage": True},
                            },
                        }
                    }
                }
            )
            yield response

        client = AsyncMock()
        client.post = post
        with self.assertRaisesRegex(RuntimeError, "Incomplete release history for owner/pack"):
            await _graphql_repositories(client, ["owner/pack"])


if __name__ == "__main__":
    unittest.main()
