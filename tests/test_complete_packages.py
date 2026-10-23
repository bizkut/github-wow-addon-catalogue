# ruff: noqa: PT009, PT027
import csv
import datetime as dt
import io
import json
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock
from zipfile import ZipFile

import aiohttp

from cataloguer import (
    Project,
    ReleaseAsset,
    ReleaseInfo,
    RepositoryInfo,
    _asset_pattern_matches,
    _parse_toc_file,
    _projects_from_graphql,
    analyse_addon_zip,
    expand_repository_releases,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "polestar-0.2.6.json").read_text(encoding="utf-8")
)
REQUIRED_FOLDERS = {
    "Polestar",
    "Polestar_Codex",
    "Polestar_Guides",
    "Polestar_Guides_ptBR",
    "Polestar_ItemDB",
    "Polestar_Names_ptBR",
    "Polestar_Nav",
    "Polestar_QuestDB",
}


def toc_archive(tocs):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for path, contents in tocs.items():
            archive.writestr(path, contents)
    return output.getvalue()


def release_node(*assets):
    return {
        "tagName": FIXTURE["source"]["tag"],
        "publishedAt": FIXTURE["source"]["published_at"],
        "isPrerelease": False,
        "isDraft": False,
        "releaseAssets": {"nodes": list(assets)},
    }


def repository_node(*releases):
    return {
        "databaseId": 1417882268,
        "name": "polestar",
        "nameWithOwner": "fitzforge/polestar",
        "url": FIXTURE["source"]["repository"],
        "description": "Leveling guides for World of Warcraft: Forever",
        "releases": {"nodes": list(releases)},
    }


class CompletePackageTest(unittest.TestCase):
    def rows(self, version="0.2.6"):
        repo = RepositoryInfo(
            id="1417882268",
            name="polestar",
            full_name="fitzforge/polestar",
            url=FIXTURE["source"]["repository"],
            description="Leveling guides for World of Warcraft: Forever",
        )
        complete = ReleaseAsset(
            f"Polestar-{version}.zip",
            FIXTURE["source"]["size"],
            FIXTURE["source"]["url"],
            analyse_addon_zip(toc_archive(FIXTURE["tocs"]), FIXTURE["source"]["asset"]),
        )
        recorder = ReleaseAsset(
            f"Polestar_Recorder-{version}.zip",
            59341,
            "https://github.com/fitzforge/polestar/releases/download/v0.2.6/"
            "Polestar_Recorder-0.2.6.zip",
            analyse_addon_zip(
                toc_archive({"Polestar_Recorder/Polestar_Recorder.toc": "## Interface: 16001"}),
                f"Polestar_Recorder-{version}.zip",
            ),
        )
        now = dt.datetime(2026, 10, 23, tzinfo=dt.UTC)
        return expand_repository_releases(
            repo,
            [ReleaseInfo(f"v{version}", now, False, False, (complete, recorder))],
            now=now,
        )

    def test_verified_complete_archive_is_one_updateable_package_not_recorder(self):
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        complete = next(row for row in rows if "Polestar" in row.folders)
        recorder = next(row for row in rows if "Polestar_Recorder" in row.folders)
        self.assertEqual(complete.id, "1417882268:polestar-zip")
        self.assertEqual(complete.name, "Polestar")
        self.assertEqual(complete.asset_name_pattern, "Polestar-*.zip")
        self.assertEqual(recorder.id, "1417882268:polestar-recorder-zip")
        self.assertEqual(recorder.name, "Polestar Recorder")
        self.assertEqual(recorder.asset_name_pattern, "Polestar_Recorder-*.zip")
        self.assertEqual(len(complete.folders), 24)
        self.assertTrue(REQUIRED_FOLDERS <= set(complete.folders))
        self.assertEqual(set(complete.flavors), {"forever"})
        self.assertEqual(set(complete.folders) & set(recorder.folders), set())
        for asset in ("Polestar_Recorder-0.2.6.zip", "Polestar_Guides_ptBR-0.2.6.zip"):
            self.assertFalse(_asset_pattern_matches(complete.asset_name_pattern, asset))
        next_complete = next(row for row in self.rows("0.2.7") if "Polestar" in row.folders)
        self.assertEqual(complete.id, next_complete.id)
        self.assertEqual(complete.asset_name_pattern, next_complete.asset_name_pattern)
        self.assertTrue(_asset_pattern_matches(complete.asset_name_pattern, "Polestar-0.2.7.zip"))
        self.assertEqual(
            Project.from_csv_row(complete.to_csv_row()).to_csv_row(), complete.to_csv_row()
        )

    def test_verified_tocs_have_closed_dependencies_and_real_archive_files(self):
        files = set(FIXTURE["files"])
        folders = {path.split("/")[0] for path in FIXTURE["tocs"]}
        self.assertEqual(folders, {path.split("/")[0] for path in files})
        for path, contents in FIXTURE["tocs"].items():
            with self.subTest(path=path):
                toc = _parse_toc_file(contents)
                self.assertEqual(toc["Interface"], "16001")
                self.assertEqual(toc["Version"], "0.2.6")
                dependencies = {
                    name.strip()
                    for name in toc.get("Dependencies", "").split(",")
                    if name.strip()
                }
                self.assertTrue(dependencies <= folders)
                if path.split("/")[0] != "Polestar":
                    self.assertIn("Polestar", dependencies)
                # These are exact TOCs and member paths from the hash-verified author ZIP.
                for line in contents.splitlines():
                    if line.strip() and not line.startswith("#"):
                        member = path.rsplit("/", 1)[0] + "/" + line.strip().replace("\\", "/")
                        self.assertIn(member, files)
        guides = _parse_toc_file(FIXTURE["tocs"]["Polestar_Guides_ptBR/Polestar_Guides_ptBR.toc"])
        names = _parse_toc_file(FIXTURE["tocs"]["Polestar_Names_ptBR/Polestar_Names_ptBR.toc"])
        self.assertEqual(guides["X-Polestar-Locale"], "ptBR")
        self.assertEqual(guides["X-Polestar-Texts"], "guides")
        self.assertEqual(names["Title"], "Polestar Names (ptBR)")
        self.assertEqual(guides["LoadOnDemand"], "1")
        self.assertEqual(names["LoadOnDemand"], "1")

    def test_generated_catalogue_contains_the_complete_archive_and_optional_recorder(self):
        with (Path(__file__).parents[1] / "addons.csv").open(encoding="utf-8", newline="") as stream:
            rows = [
                Project.from_csv_row(row)
                for row in csv.DictReader(stream)
                if row["full_name"] == "fitzforge/polestar"
            ]
        expected = {row.id: row for row in self.rows()}
        self.assertEqual(set(expected), {row.id for row in rows})
        for row in rows:
            self.assertEqual(row.asset_name_pattern, expected[row.id].asset_name_pattern)
            self.assertEqual(row.folders, expected[row.id].folders)
            self.assertEqual(set(row.flavors), set(expected[row.id].flavors))

    def test_language_and_faction_stream_selectors_stay_distinct(self):
        repo = RepositoryInfo(
            id="123",
            name="spoken-wow",
            full_name="owner/spoken",
            url="https://github.com/owner/spoken",
            description="Audio",
        )
        streams = {
            "quests-audio/": "SpokenQuestsAudioAll",
            "quests-audio-ptBR/": "SpokenQuestsAudio_ptBR",
            "quests-audio-alliance/": "SpokenQuestsAudioAlliance",
            "quests-audio-horde/": "SpokenQuestsAudioHorde",
            "quests-audio-shared/": "SpokenQuestsAudioShared",
        }
        now = dt.datetime(2026, 10, 23, tzinfo=dt.UTC)
        releases = [
            ReleaseInfo(
                prefix + "v2.0.0", now, False, False,
                (
                    ReleaseAsset(
                        folder + "-2.0.0.zip", 100, "https://example/" + folder,
                        analyse_addon_zip(
                            toc_archive({folder + "/" + folder + ".toc": "## Interface: 16001"}),
                            folder + ".zip",
                        ),
                    ),
                ),
            )
            for prefix, folder in streams.items()
        ]
        rows = expand_repository_releases(repo, releases, now=now)
        self.assertEqual(len({row.id for row in rows}), len(streams))
        self.assertEqual({row.release_tag_prefix for row in rows}, set(streams))
        for row in rows:
            self.assertEqual(row.folders, (streams[row.release_tag_prefix],))
            matched = {
                asset.name
                for release in releases
                for asset in release.assets
                if _asset_pattern_matches(row.asset_name_pattern, asset.name)
            }
            self.assertEqual(matched, {streams[row.release_tag_prefix] + "-2.0.0.zip"})


class IncompletePackageDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def discover(self, failed_url, *, existing=(), other_repository=False):
        archives = {
            FIXTURE["source"]["url"]: toc_archive(FIXTURE["tocs"]),
            "https://example/recorder.zip": toc_archive(
                {"Polestar_Recorder/Polestar_Recorder.toc": "## Interface: 16001"}
            ),
        }
        repository = repository_node(
            release_node(*(
                {"name": name, "size": len(archives[url]), "downloadUrl": url}
                for name, url in (
                    (FIXTURE["source"]["asset"], FIXTURE["source"]["url"]),
                    ("Polestar_Recorder-0.2.6.zip", "https://example/recorder.zip"),
                )
            ))
        )
        repositories = [repository]
        if other_repository:
            url = "https://example/other.zip"
            archives[url] = toc_archive({"Other/Other.toc": "## Interface: 16001"})
            other = repository_node(
                release_node(
                    {"name": "Other-0.2.6.zip", "size": len(archives[url]), "downloadUrl": url}
                )
            )
            other.update(
                databaseId=123, name="Other", nameWithOwner="owner/other",
                url="https://github.com/owner/other",
            )
            repositories.append(other)

        @asynccontextmanager
        async def get(url, *, headers, expire_after):
            if url == failed_url:
                raise aiohttp.ClientPayloadError("incomplete body")
            start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            response = AsyncMock(status=206)
            response.read.return_value = archives[url][start : end + 1]
            yield response

        return await _projects_from_graphql(
            AsyncMock(), get, repositories, existing=existing
        )

    async def test_failed_complete_asset_inspection_never_publishes_recorder_as_core(self):
        rows = await self.discover(FIXTURE["source"]["url"], other_repository=True)
        recorder = next(row for row in rows if row.full_name == "fitzforge/polestar")
        self.assertEqual(recorder.id, "1417882268:polestar-recorder-zip")
        self.assertEqual(recorder.name, "Polestar Recorder")
        self.assertEqual(recorder.folders, ("Polestar_Recorder",))
        self.assertEqual(len(rows), 2)
        self.assertIn("owner/other", {row.full_name for row in rows})

    async def test_failed_optional_recorder_does_not_hide_complete_package(self):
        rows = await self.discover("https://example/recorder.zip")
        self.assertEqual(len(rows), 1)
        complete = next(iter(rows))
        self.assertEqual(complete.id, "1417882268:polestar-zip")
        self.assertTrue(REQUIRED_FOLDERS <= set(complete.folders))
        self.assertEqual(len(complete.folders), 24)

    async def test_failed_asset_keeps_only_its_last_known_catalogue_row(self):
        # Round-trip the real CSV type path: cached projects must remain hashable.
        existing = [
            Project.from_csv_row(row.to_csv_row()) for row in CompletePackageTest().rows()
        ]
        for failed_url in (FIXTURE["source"]["url"], "https://example/recorder.zip"):
            with self.subTest(failed_url=failed_url):
                rows = await self.discover(failed_url, existing=existing)
                self.assertEqual(len(rows), 2)
                pattern = (
                    "Polestar-*.zip" if failed_url == FIXTURE["source"]["url"]
                    else "Polestar_Recorder-*.zip"
                )
                preserved = next(row for row in rows if row.asset_name_pattern == pattern)
                previous = next(row for row in existing if row.id == preserved.id)
                self.assertEqual(preserved, previous)
                updated = next(row for row in rows if row.id != preserved.id)
                self.assertGreater(updated.last_seen, previous.last_seen)

    async def test_successful_inspection_replaces_all_previous_repository_rows(self):
        existing = CompletePackageTest().rows()
        rows = await self.discover(None, existing=existing)
        self.assertEqual({row.id for row in rows}, {row.id for row in existing})
        self.assertTrue(all(row.last_seen > existing[0].last_seen for row in rows))


class PackageVariantFailureTest(unittest.IsolatedAsyncioTestCase):
    async def discover(self, *, failed=(), existing=(), voice=False, streams=("",)):
        archives = {
            "LoreForever.zip": toc_archive(
                {"LoreForever/LoreForever.toc": "## Interface: 16001"}
            ),
            "LoreForever-nolib.zip": toc_archive(
                {"LoreForever/LoreForever.toc": "## Interface: 16001"}
            ),
        }
        if voice:
            archives["LoreForever_Voice.zip"] = toc_archive(
                {"LoreVoice/LoreVoice.toc": "## Interface: 16001"}
            )
        repository = repository_node(*(
            release_node(*(
                {
                    "name": name,
                    "size": len(contents),
                    "downloadUrl": "https://example/" + stream + name,
                }
                for name, contents in archives.items()
            )) | {"tagName": stream + "v0.2.6"}
            for stream in streams
        ))
        repository.update(
            databaseId=1234, name="Pack", nameWithOwner="owner/pack",
            url="https://github.com/owner/pack",
        )

        @asynccontextmanager
        async def get(url, *, headers, expire_after):
            identity = url.removeprefix("https://example/")
            if identity in failed:
                raise aiohttp.ClientPayloadError("incomplete body")
            contents = archives[identity.rsplit("/", 1)[-1]]
            start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            response = AsyncMock(status=206)
            response.read.return_value = contents[start : end + 1]
            yield response

        return await _projects_from_graphql(
            AsyncMock(), get, [repository], existing=existing
        )

    async def test_failed_nolib_variant_preserves_single_package_identity(self):
        healthy = await self.discover()
        self.assertEqual(len(healthy), 1)
        previous = next(iter(healthy))
        self.assertEqual(previous.id, "1234")
        degraded = await self.discover(
            failed=("LoreForever-nolib.zip",), existing=tuple(healthy)
        )
        self.assertEqual(len(degraded), 1)
        current = next(iter(degraded))
        self.assertEqual(current.id, previous.id)
        self.assertEqual(current.name, previous.name)
        self.assertEqual(current.asset_name_pattern, previous.asset_name_pattern)
        recovered = await self.discover(existing=tuple(degraded))
        self.assertEqual(next(iter(recovered)).id, previous.id)

    async def test_failed_normal_variant_does_not_duplicate_successful_nolib_group(self):
        healthy = await self.discover(voice=True)
        self.assertEqual(len(healthy), 2)
        degraded = await self.discover(
            failed=("LoreForever.zip",), existing=tuple(healthy), voice=True
        )
        self.assertEqual(len(degraded), 2)
        core = [row for row in degraded if row.folders == ("LoreForever",)]
        self.assertEqual(len(core), 1)
        self.assertEqual(core[0].asset_name_pattern, "LoreForever-nolib.zip")
        previous = next(row for row in healthy if row.folders == ("LoreForever",))
        self.assertEqual(core[0].id, previous.id)
        self.assertEqual(core[0].name, previous.name)

    async def test_retention_deduplicates_folders_only_within_same_stream(self):
        streams = ("quests-audio/", "quests-audio-ptBR/", "quests-audio-horde/")
        healthy = await self.discover(streams=streams)
        degraded = await self.discover(
            failed=(
                "quests-audio/LoreForever.zip",
                "quests-audio/LoreForever-nolib.zip",
                "quests-audio-ptBR/LoreForever.zip",
            ),
            existing=tuple(healthy), streams=streams,
        )
        self.assertEqual(len(degraded), 3)
        self.assertEqual({row.release_tag_prefix for row in degraded}, set(streams))
        english = next(row for row in healthy if row.release_tag_prefix == streams[0])
        self.assertIn(english, degraded)
        portuguese = next(row for row in degraded if row.release_tag_prefix == streams[1])
        self.assertEqual(portuguese.asset_name_pattern, "LoreForever-nolib.zip")
