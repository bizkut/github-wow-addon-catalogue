from __future__ import annotations

import asyncio
import datetime as dt
import enum
import io
import json
import re
import struct
import zlib
from collections.abc import Iterable, Mapping, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
)
from typing import Any, Literal, NewType, Protocol
from zipfile import BadZipFile, ZipFile

import aiohttp
import structlog
from aiohttp_client_cache.cache_control import ExpirationPatterns
from attrs import evolve, field, fields, frozen
from cattrs import BaseValidationError, Converter
from yarl import URL

logger = structlog.get_logger()


REPO_EXCLUDES = (
    "alchem1ster/AddOns-Update-Tool",  # Not an add-on
    "alchem1ster/AddOnsFixer",  # Not an add-on
    "Aviana/",
    "BilboTheGreedy/Azerite",  # Not an add-on
    "blazer404/TargetCharmsRe",  # Fork
    "Centias/BankItems",  # Fork
    "DaMitchell/HelloWorld",  # Dummy add-on
    "dratr/BattlePetCount",  # Fork
    "erikg/WoWStatTracker",  # Not an add-on
    "Expensify/App",  # Not an add-on
    "ftsef/queue-pop-notification",  # Not an add-on
    "gorilla-devs/",  # Minecraft stuff
    "HappyRot/AddOns",  # Compilation
    "hippuli/",  # Fork galore
    "itzg/mc-image-helper",  # Not an add-on
    "JsMacros/",  # Minecraft stuff
    "juraj-hrivnak/Underdog",  # Minecraft stuff
    "kamoo1/Kamoo-s-TSM-App",  # Not an add-on
    "Kirri777/WorldQuestsList",  # Fork
    "layday/wow-addon-template",  # Template
    "link-fgfgui/mod-downloader",  # Not an add-on
    "livepeer/",  # Minecraft stuff
    "lowlee/MikScrollingBattleText",  # Fork
    "lowlee/MSBTOptions",  # Fork
    "McTalian-WoW-Addons/wow-build-tools",  # Not an add-on
    "Mctalian/wow-build-tools",  # Not an add-on
    "medi8tor/Addons",  # Add-on pack
    "MikeD89/KarazhanChess",  # Hijacking BigWigs' TOC IDs, probably by accident
    "MinecraftLabs/beyond-adventures-modpack",  # Not an add-on
    "ogri-la/elvui",  # Mirror
    "ogri-la/tukui",  # Mirror
    "Oppzippy/HuokanGoldLogger",  # Archived
    "pinged-eu/wow-addon-helloworld",  # Dummy add-on
    "rePublic-Studios/rPLauncher",  # Minecraft stuff
    "Rmkrs/WowSync",  # Not an add-on
    "smashedr/MethodAltManager",  # Fork
    "szjunklol/Accountant",  # Fork
    "unix/curseforge-release",  # Template
    "unrealshape/AddOns",  # Add-on compilation
    "vicitafirea/InterfaceColors-Addon",  # Custom client add-on
    "vicitafirea/TimeOfDayIndicator-AddOn",  # Custom client add-on
    "vicitafirea/TurtleHardcoreMessages-AddOn",  # Custom client add-on
    "vicitafirea/WarcraftUI-UpperBar-AddOn",  # Custom client add-on
    "VM-Chinese-translate-group/FTB-Skies-2-Chinese",  # Not an add-on
    "wagyourtail/JsMacros",  # More Minecraft stuff
    "WOWRainbowUI/RainbowUI-Era",  # Massive add-on bundle (from torkus)
    "WOWRainbowUI/RainbowUI-Retail",  # Massive add-on bundle (from torkus)
    "WowUp/WowUp",  # Not an add-on
    "XiconQoo/RETabBinder",  # Fork for dead version of the game
    "ynazar1/Arh",  # Fork
    "yuanzhou029/APK",  # Not an add-on
)


USER_AGENT = "github-wow-addon-catalogue (+https://github.com/layday/github-wow-addon-catalogue)"

_API_URL = URL("https://api.github.com/")

_SEARCH_INTERVAL_HOURS = 2

_CACHE_INDEFINITELY = -1
_EXPIRE_URLS: ExpirationPatterns = {
    f"{_API_URL.host}/search": dt.timedelta(hours=_SEARCH_INTERVAL_HOURS),
    f"{_API_URL.host}/repos/*/releases": dt.timedelta(hours=1),
}


class _Get(Protocol):
    def __call__(
        self, url: str | URL, **kwargs: Any
    ) -> AbstractAsyncContextManager[aiohttp.ClientResponse]: ...


class ReleaseJsonFlavor(enum.StrEnum):
    mainline = "mainline"
    classic = "classic"
    bcc = "bcc"
    wrath = "wrath"
    cata = "cata"
    mists = "mists"
    titan = "titan"
    forever = "forever"


@frozen
class ReleaseJson:
    releases: list[ReleaseJsonRelease]


@frozen
class ReleaseJsonRelease:
    filename: str
    nolib: bool
    metadata: list[ReleaseJsonReleaseMetadata]


@frozen
class ReleaseJsonReleaseMetadata:
    flavor: ReleaseJsonFlavor
    interface: int


_release_json_converter = Converter()


_TOC_ALIASES = {
    **ReleaseJsonFlavor.__members__,
    "vanilla": ReleaseJsonFlavor.classic,
    "tbc": ReleaseJsonFlavor.bcc,
    "wotlkc": ReleaseJsonFlavor.wrath,
    "camelot": ReleaseJsonFlavor.forever,
}

_top_level_toc_name_pattern = re.compile(
    rf"""
        ^
        (?P<name>[^/]+)
        [/]
        (?P=name)
        (?:[-_](?P<flavor>{"|".join(map(re.escape, _TOC_ALIASES))}))?
        \.toc
        $
    """,
    flags=re.I | re.X,
)

INTERFACE_RANGES_TO_FLAVORS = {
    range(1_00_00, 1_13_00): ReleaseJsonFlavor.mainline,
    range(1_13_00, 1_60_00): ReleaseJsonFlavor.classic,
    range(1_60_00, 2_00_00): ReleaseJsonFlavor.forever,
    range(2_00_00, 2_05_00): ReleaseJsonFlavor.mainline,
    range(2_05_00, 3_00_00): ReleaseJsonFlavor.bcc,
    range(3_00_00, 3_04_00): ReleaseJsonFlavor.mainline,
    range(3_04_00, 4_00_00): ReleaseJsonFlavor.wrath,
    range(4_00_00, 4_04_00): ReleaseJsonFlavor.mainline,
    range(4_04_00, 5_00_00): ReleaseJsonFlavor.cata,
    range(5_00_00, 5_05_00): ReleaseJsonFlavor.mainline,
    range(5_05_00, 6_00_00): ReleaseJsonFlavor.mists,
    range(6_00_00, 12_00_00): ReleaseJsonFlavor.mainline,
}


ProjectFlavors = NewType("ProjectFlavors", frozenset[ReleaseJsonFlavor])


@frozen(frozen=True, kw_only=True)
class Project:
    id: str = field(converter=str)
    name: str
    full_name: str
    url: str
    description: str | None
    last_updated: dt.datetime
    flavors: ProjectFlavors
    curse_id: str | None
    wago_id: str | None
    wowi_id: str | None
    has_release_json: bool
    last_seen: dt.datetime
    release_tag_prefix: str | None = None
    asset_name_pattern: str | None = None
    folders: tuple[str, ...] = ()

    @classmethod
    def from_csv_row(cls, values: Mapping[str, Any]):
        row = dict(values)
        for name in (
            "description",
            "curse_id",
            "wago_id",
            "wowi_id",
            "release_tag_prefix",
            "asset_name_pattern",
        ):
            row[name] = row.get(name) or None
        row["folders"] = tuple(filter(None, row.get("folders", "").split(",")))
        return _addons_csv_converter.structure(row, cls)

    def to_csv_row(self):
        row = _addons_csv_converter.unstructure(self)
        row["folders"] = ",".join(self.folders)
        return row


PROJECT_FIELD_NAMES = tuple(f.name for f in fields(Project))

_addons_csv_converter = Converter()
_addons_csv_converter.register_unstructure_hook(dt.datetime, lambda v: v.isoformat())
_addons_csv_converter.register_structure_hook(
    dt.datetime, lambda v, _: dt.datetime.fromisoformat(v)
)
_addons_csv_converter.register_unstructure_hook(ProjectFlavors, lambda v: ",".join(sorted(v)))
_addons_csv_converter.register_structure_hook(
    bool,
    lambda v, _: v == "True",
)
_addons_csv_converter.register_structure_hook(
    ProjectFlavors,
    lambda v, _: ProjectFlavors(frozenset(ReleaseJsonFlavor(f) for f in v.split(",") if f)),
)


def _parse_toc_file(contents: str):
    return {
        k: v
        for e in contents.splitlines()
        if e.startswith("##")
        for k, v in ((i.strip() for i in e.lstrip("#").partition(":")[::2]),)
        if k
    }


def _extract_interface_numbers(interfaces: str):
    for interface in interfaces.split(","):
        try:
            yield int(interface)
        except ValueError:
            continue


def _interface_numbers_to_flavours(interface_numbers: Iterable[int]):
    for interface_number in interface_numbers:
        for interface_range, flavor in INTERFACE_RANGES_TO_FLAVORS.items():
            if interface_number in interface_range:
                yield flavor
                break


@frozen
class ArchiveAnalysis:
    flavors: ProjectFlavors
    folders: tuple[str, ...]
    curse_id: str | None = None
    wago_id: str | None = None
    wowi_id: str | None = None


@frozen
class ReleaseAsset:
    name: str
    size: int
    download_url: str
    analysis: ArchiveAnalysis | None = None
    inspection_failed: bool = False


@frozen
class ReleaseInfo:
    tag_name: str
    published_at: dt.datetime
    prerelease: bool
    draft: bool
    assets: tuple[ReleaseAsset, ...] = field(converter=tuple)


@frozen
class RepositoryInfo:
    id: str = field(converter=str)
    name: str
    full_name: str
    url: str
    description: str | None


def analyse_addon_zip(contents: bytes, filename: str) -> ArchiveAnalysis | None:
    del filename
    try:
        with ZipFile(io.BytesIO(contents)) as addon_zip:
            toc_rows: list[tuple[str, ReleaseJsonFlavor | None, dict[str, str]]] = []
            for path in addon_zip.namelist():
                match = _top_level_toc_name_pattern.fullmatch(path)
                if not match:
                    continue
                try:
                    toc = _parse_toc_file(addon_zip.read(path).decode("utf-8-sig"))
                except (KeyError, UnicodeDecodeError):
                    continue
                suffix = match["flavor"]
                toc_rows.append(
                    (
                        match["name"],
                        _TOC_ALIASES.get(suffix.lower()) if suffix else None,
                        toc,
                    )
                )
    except (BadZipFile, OSError):
        return None
    if not toc_rows:
        return None
    flavors = {
        flavor
        for _, suffix_flavor, toc in toc_rows
        for flavor in (
            (suffix_flavor,)
            if suffix_flavor is not None
            else tuple(
                _interface_numbers_to_flavours(
                    _extract_interface_numbers(toc.get("Interface", ""))
                )
            )
        )
    }
    folders = tuple(sorted({folder for folder, _, _ in toc_rows}, key=str.lower))
    if not flavors or not folders:
        return None

    def first(key: str) -> str | None:
        return next((toc[key] for _, _, toc in toc_rows if toc.get(key)), None)

    return ArchiveAnalysis(
        flavors=ProjectFlavors(frozenset(flavors)),
        folders=folders,
        curse_id=first("X-Curse-Project-ID"),
        wago_id=first("X-Wago-ID"),
        wowi_id=first("X-WoWI-ID"),
    )


_STREAM_TAG = re.compile(r"^(.+)/v\d", re.I)
_LANGUAGE_NAMES = {
    "dede": "German",
    "eses": "Spanish (Europe)",
    "esmx": "Spanish (Mexico)",
    "frfr": "French",
    "itit": "Italian",
    "kokr": "Korean",
    "ptbr": "Portuguese",
    "ruru": "Russian",
    "zhcn": "Chinese (Simplified)",
    "zhtw": "Chinese (Traditional)",
}


def _display_words(value: str) -> str:
    raw_parts = re.split(r"[-_]", value)
    language = _LANGUAGE_NAMES.get(raw_parts[-1].lower())
    if language:
        raw_parts = raw_parts[:-1]
    parts = [
        word
        for part in raw_parts
        for word in re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", part).split()
    ]
    words = " ".join(part.capitalize() for part in parts if part)
    if language:
        return f"{words} ({language})"
    return f"{words} (All)" if value.lower().endswith("-audio") else words


def _asset_pattern_matches(pattern: str, name: str) -> bool:
    first, separator, second = pattern.lower().partition("*")
    if not separator:
        return name.lower() == first
    if "*" in second:
        return False
    name = name.lower()
    return (
        len(name) >= len(first) + len(second)
        and name.startswith(first)
        and name.endswith(second)
    )


def _version_free_asset_pattern(release: ReleaseInfo, asset: ReleaseAsset) -> str:
    version = release.tag_name.rsplit("/", 1)[-1].removeprefix("v").removeprefix("V")
    variants = {
        version,
        version.replace(".", "-"),
        version.replace(".", "_"),
        "v" + version,
        "V" + version,
    }
    zip_names = [
        candidate.name
        for candidate in release.assets
        if candidate.name.lower().endswith(".zip")
    ]
    for variant in sorted(filter(None, variants), key=len, reverse=True):
        match = re.search(re.escape(variant), asset.name, re.I)
        if not match:
            continue
        pattern = asset.name[: match.start()] + "*" + asset.name[match.end() :]
        if sum(_asset_pattern_matches(pattern, name) for name in zip_names) == 1:
            return pattern
    logger.warning(
        "release asset needs an exact versioned selector",
        release=release.tag_name,
        asset=asset.name,
    )
    return asset.name


def _release_rows(
    repo: RepositoryInfo,
    release: ReleaseInfo,
    now: dt.datetime,
    tag_prefix: str | None,
) -> list[Project]:
    groups: dict[tuple[str, ...], list[ReleaseAsset]] = {}
    for asset in release.assets:
        if asset.analysis is not None:
            groups.setdefault(asset.analysis.folders, []).append(asset)
    split_assets = len(groups) > 1
    incomplete = any(asset.inspection_failed for asset in release.assets)
    rows: list[Project] = []
    for folders, variants in groups.items():
        asset = next(
            (candidate for candidate in variants if "nolib" not in candidate.name.lower()),
            variants[0],
        )
        analysis = asset.analysis
        assert analysis is not None
        pattern = _version_free_asset_pattern(release, asset)
        identity = tag_prefix.rstrip("/") if tag_prefix else pattern
        safe_identity = re.sub(r"[^a-z0-9]+", "-", identity.lower()).strip("-")
        if tag_prefix:
            name = _display_words(identity)
            if repo.name.lower().startswith("spoken") and not name.lower().startswith("spoken"):
                name = f"Spoken {name}"
        elif split_assets or incomplete:
            name = _display_words(folders[0])
        else:
            name = repo.name
        size_mib = max(1, round(asset.size / (1024 * 1024)))
        description = (repo.description or "GitHub author release").strip()
        description = f"{description} - {size_mib} MiB"
        # Without a complete inspection or matching cached identity, use the
        # observed asset's identity rather than mislabelling it as the base add-on.
        row_id = (
            repo.id if not tag_prefix and not split_assets and not incomplete
            else f"{repo.id}:{safe_identity}"
        )
        rows.append(
            Project(
                id=row_id,
                name=name,
                full_name=repo.full_name,
                url=repo.url,
                description=description,
                last_updated=release.published_at,
                flavors=analysis.flavors,
                curse_id=analysis.curse_id,
                wago_id=analysis.wago_id,
                wowi_id=analysis.wowi_id,
                has_release_json=False,
                last_seen=now,
                release_tag_prefix=tag_prefix,
                asset_name_pattern=pattern,
                folders=folders,
            )
        )
    return rows


def expand_repository_releases(
    repo: RepositoryInfo,
    releases: Sequence[ReleaseInfo],
    *,
    now: dt.datetime,
) -> list[Project]:
    eligible = [release for release in releases if not release.draft and not release.prerelease]
    streams: dict[str, list[ReleaseInfo]] = {}
    ordinary: list[ReleaseInfo] = []
    for release in eligible:
        match = _STREAM_TAG.match(release.tag_name)
        if match:
            streams.setdefault(match[1] + "/", []).append(release)
        else:
            ordinary.append(release)
    rows: list[Project] = []
    for prefix, candidates in sorted(streams.items()):
        latest = max(candidates, key=lambda item: item.published_at)
        rows.extend(_release_rows(repo, latest, now, prefix))
    if ordinary:
        latest = max(ordinary, key=lambda item: item.published_at)
        rows.extend(_release_rows(repo, latest, now, None))
    return rows


def _selected_releases(releases: Sequence[ReleaseInfo]) -> list[ReleaseInfo]:
    eligible = [release for release in releases if not release.draft and not release.prerelease]
    streams: dict[str, list[ReleaseInfo]] = {}
    ordinary: list[ReleaseInfo] = []
    for release in eligible:
        match = _STREAM_TAG.match(release.tag_name)
        if match:
            streams.setdefault(match[1] + "/", []).append(release)
        else:
            ordinary.append(release)
    selected = [
        max(candidates, key=lambda item: item.published_at) for candidates in streams.values()
    ]
    if ordinary:
        selected.append(max(ordinary, key=lambda item: item.published_at))
    return selected


async def _range_bytes(get: _Get, url: str, start: int, end: int) -> bytes:
    async with get(
        url, headers={"Range": f"bytes={start}-{end}"}, expire_after=0
    ) as response:
        if response.status == 206:
            return await response.read()
        if response.status == 200 and response.content_length is not None:
            if response.content_length <= 8 * 1024 * 1024:
                body = await response.read()
                return body[start : end + 1]
        raise ValueError(f"asset server ignored bounded range for {url}")  # noqa: TRY003


async def _analyse_remote_zip(get: _Get, asset: ReleaseAsset) -> ArchiveAnalysis | None:
    if asset.size <= 0:
        return None
    tail_size = min(asset.size, 128 * 1024)
    tail = await _range_bytes(get, asset.download_url, asset.size - tail_size, asset.size - 1)
    marker = tail.rfind(b"PK\x05\x06")
    if marker < 0 or len(tail) - marker < 22:
        return None
    _, _, _, _, entries, central_size, central_offset, _ = struct.unpack_from(
        "<4s4H2IH", tail, marker
    )
    if not entries or central_size > 16 * 1024 * 1024:
        return None
    central = await _range_bytes(
        get, asset.download_url, central_offset, central_offset + central_size - 1
    )
    members: list[tuple[str, int, int, int]] = []
    cursor = 0
    while cursor + 46 <= len(central):
        values = struct.unpack_from("<4s6H3I5H2I", central, cursor)
        if values[0] != b"PK\x01\x02":
            break
        compression, compressed_size = values[4], values[8]
        name_length, extra_length, comment_length, local_offset = (
            values[10],
            values[11],
            values[12],
            values[16],
        )
        name_start = cursor + 46
        name = central[name_start : name_start + name_length].decode("utf-8", "replace")
        if _top_level_toc_name_pattern.fullmatch(name) and compressed_size <= 1024 * 1024:
            members.append((name, local_offset, compression, compressed_size))
        cursor = name_start + name_length + extra_length + comment_length
    if not members:
        return None
    toc_files: list[tuple[str, str]] = []
    for name, local_offset, compression, compressed_size in members:
        header = await _range_bytes(get, asset.download_url, local_offset, local_offset + 29)
        if len(header) != 30 or header[:4] != b"PK\x03\x04":
            return None
        local = struct.unpack("<4s5H3I2H", header)
        name_length, extra_length = local[9], local[10]
        data_start = local_offset + 30 + name_length + extra_length
        payload = await _range_bytes(
            get, asset.download_url, data_start, data_start + compressed_size - 1
        )
        if compression == 0:
            decoded = payload
        elif compression == 8:
            decoded = zlib.decompress(payload, -15)
        else:
            continue
        try:
            toc_files.append((name, decoded.decode("utf-8-sig")))
        except UnicodeDecodeError:
            continue
    return analyse_addon_zip(addon_zip_bytes(toc_files), asset.name) if toc_files else None


def addon_zip_bytes(files: Sequence[tuple[str, str]]) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for name, contents in files:
            archive.writestr(name, contents)
    return output.getvalue()


async def _graphql_repositories(
    client: aiohttp.ClientSession,
    names: Sequence[str],
) -> list[dict[str, Any]]:
    async def query(
        batch: Sequence[str], release_count: int = 100, cursor: str | None = None
    ) -> list[tuple[dict[str, Any], int]]:
        declarations = ", ".join(
            f"$owner{i}:String!,$name{i}:String!" for i in range(len(batch))
        )
        fields = "\n".join(
            f"r{i}:repository(owner:$owner{i},name:$name{i}){{"
            "databaseId name nameWithOwner url description "
            f"releases(first:{release_count},after:$cursor,"
            "orderBy:{field:CREATED_AT,direction:DESC}){"
            "pageInfo{endCursor hasNextPage} nodes{tagName publishedAt isPrerelease isDraft "
            "releaseAssets(first:100){nodes{name size downloadUrl}}}}}"
            for i in range(len(batch))
        )
        variables: dict[str, str | None] = {
            key: value
            for i, full_name in enumerate(batch)
            for key, value in (
                (f"owner{i}", full_name.split("/", 1)[0]),
                (f"name{i}", full_name.split("/", 1)[1]),
            )
        }
        variables["cursor"] = cursor
        async with client.post(
            "https://api.github.com/graphql",
            json={
                "query": f"query({declarations},$cursor:String){{{fields}}}",
                "variables": variables,
            },
        ) as response:
            payload = await response.json()
        if response.status < 400 and not payload.get("errors"):
            return [(value, release_count) for value in payload["data"].values() if value]
        if len(batch) > 1:
            middle = len(batch) // 2
            left, right = await asyncio.gather(
                query(batch[:middle], release_count, cursor),
                query(batch[middle:], release_count, cursor),
            )
            return left + right
        if release_count > 20:
            return await query(batch, 20, cursor)
        logger.warning("GitHub GraphQL repository skipped", repository=batch[0],
                       errors=payload.get("errors", response.status))
        return []

    rows: list[dict[str, Any]] = []
    for batch_start in range(0, len(names), 5):
        for row, release_count in await query(names[batch_start : batch_start + 5]):
            releases = row["releases"]
            while releases["pageInfo"]["hasNextPage"]:
                page = await query(
                    [row["nameWithOwner"]], release_count, releases["pageInfo"]["endCursor"]
                )
                if not page:
                    message = f"Incomplete release history for {row['nameWithOwner']}"
                    raise RuntimeError(message)
                next_row, release_count = page[0]
                releases["nodes"].extend(next_row["releases"]["nodes"])
                releases["pageInfo"] = next_row["releases"]["pageInfo"]
            rows.append(row)
    return rows


async def _projects_from_graphql(
    client: aiohttp.ClientSession,
    get: _Get,
    repositories: Sequence[dict[str, Any]],
    *,
    existing: Sequence[Project] = (),
) -> set[Project]:
    semaphore = asyncio.Semaphore(16)

    async def inspect(asset: ReleaseAsset) -> ReleaseAsset:
        if not asset.name.lower().endswith(".zip"):
            return asset
        async with semaphore:
            try:
                analysis = await _analyse_remote_zip(get, asset)
            except (aiohttp.ClientError, OSError, ValueError, zlib.error) as error:
                logger.warning(
                    "unable to inspect add-on ZIP; preserving matching catalogue rows",
                    asset=asset.download_url,
                    error=str(error),
                )
                return ReleaseAsset(
                    asset.name, asset.size, asset.download_url, inspection_failed=True
                )
        return ReleaseAsset(asset.name, asset.size, asset.download_url, analysis)

    del client
    now = dt.datetime.now(dt.UTC)

    async def process(row: dict[str, Any]) -> set[Project]:
        repo = RepositoryInfo(
            id=row["databaseId"],
            name=row["name"],
            full_name=row["nameWithOwner"],
            url=row["url"],
            description=row["description"],
        )
        releases = [
            ReleaseInfo(
                tag_name=node["tagName"],
                published_at=dt.datetime.fromisoformat(
                    node["publishedAt"].replace("Z", "+00:00")
                ),
                prerelease=node["isPrerelease"],
                draft=node["isDraft"],
                assets=tuple(
                    ReleaseAsset(asset["name"], asset["size"], asset["downloadUrl"])
                    for asset in node["releaseAssets"]["nodes"]
                ),
            )
            for node in row["releases"]["nodes"]
            if node["publishedAt"]
        ]
        selected = _selected_releases(releases)

        async def inspect_release(release: ReleaseInfo) -> ReleaseInfo:
            return ReleaseInfo(
                tag_name=release.tag_name,
                published_at=release.published_at,
                prerelease=release.prerelease,
                draft=release.draft,
                assets=tuple(await asyncio.gather(
                    *(inspect(asset) for asset in release.assets))),
            )

        inspected = await asyncio.gather(*(inspect_release(release) for release in selected))
        projects = set(expand_repository_releases(repo, inspected, now=now))
        failed = [
            (release.tag_name, asset.name)
            for release in inspected
            for asset in release.assets
            if asset.inspection_failed
        ]
        previous = [
            project for project in existing
            if project.full_name.lower() == repo.full_name.lower()
        ]
        if failed:
            # Missing ZIPs do not prove there are distinct packages: a failed
            # sibling may be another variant of the same folder group (nolib).
            # Preserve an established group's identity, but use its freshly
            # inspected selector, folders and timestamps.
            stable: set[Project] = set()
            for project in projects:
                cached = next((
                    candidate for candidate in previous
                    if candidate.folders == project.folders
                    and candidate.release_tag_prefix == project.release_tag_prefix
                ), None)
                stable.add(
                    evolve(project, id=cached.id, name=cached.name)
                    if cached is not None else project
                )
            projects = stable
        for project in previous:
            # Retain only the unavailable asset's last known metadata, not stale
            # rows for other streams/assets. Legacy rows cannot identify an asset.
            if project.asset_name_pattern and any(
                (
                    tag.startswith(project.release_tag_prefix)
                    if project.release_tag_prefix
                    else not _STREAM_TAG.match(tag)
                )
                and _asset_pattern_matches(project.asset_name_pattern, name)
                for tag, name in failed
            ):
                if not any(
                    row.id == project.id or (
                        row.folders == project.folders
                        and row.release_tag_prefix == project.release_tag_prefix
                    )
                    for row in projects
                ):
                    projects.add(project)
            elif failed and not projects and not project.asset_name_pattern:
                projects.add(project)
        return projects

    output: set[Project] = set()
    for projects in await asyncio.gather(*(process(row) for row in repositories)):
        output.update(projects)
    return output


async def _find_addon_repos(
    get: _Get,
    queries_by_endpoint: Sequence[
        tuple[Literal["code", "repositories", "user_repositories"], str]
    ],
):
    for endpoint, query in queries_by_endpoint:
        search_url = (
            (_API_URL / "users" / query / "repos")
            if endpoint == "user_repositories"
            else (_API_URL / "search" / endpoint).with_query(q=query)
        )
        search_url = search_url.extend_query(per_page=100)
        while True:
            async with get(search_url) as response:
                content = await response.json()

            match endpoint:
                case "code":
                    for item in content["items"]:
                        yield item["repository"]
                case "repositories":
                    for item in content["items"]:
                        yield item
                case "user_repositories":
                    for item in content:
                        yield item

            next_url = response.links.get("next")
            if next_url is None:
                break
            search_url = next_url["url"]


async def _extract_project_ids_from_toc_files(get: _Get, url: str, zip_filename: str):
    async with get(url) as file_response:
        file = await file_response.read()

    def is_valid_toc(toc_filename: str):
        match = _top_level_toc_name_pattern.match(toc_filename)
        if match:
            return zip_filename.startswith(match["name"].lstrip("!_"))

    with ZipFile(io.BytesIO(file)) as addon_zip:
        toc_files_contents = [
            addon_zip.read(n).decode("utf-8-sig") for n in addon_zip.namelist() if is_valid_toc(n)
        ]

    if toc_files_contents:
        tocs = (_parse_toc_file(c) for c in toc_files_contents)
        toc_ids = (
            (
                t.get("X-Curse-Project-ID"),
                t.get("X-Wago-ID"),
                t.get("X-WoWI-ID"),
            )
            for t in tocs
        )
        toc_ids_by_source: zip[tuple[str, ...]] = zip(*toc_ids)
        project_ids = (next(filter(None, s), None) for s in toc_ids_by_source)

    else:
        logger.warning(f"unable to find conformant TOC files in {url}")
        project_ids = (None,) * 3

    return dict(zip(("curse_id", "wago_id", "wowi_id"), project_ids))


async def _extract_game_flavors_from_tocs(get: _Get, release_archives: Sequence[dict[str, Any]]):
    for release_archive in release_archives:
        async with get(release_archive["browser_download_url"]) as file_response:
            file = await file_response.read()

        with ZipFile(io.BytesIO(file)) as addon_zip:
            toc_names = [
                (n, _TOC_ALIASES.get(m["flavor"]))
                for n in addon_zip.namelist()
                for m in (_top_level_toc_name_pattern.match(n),)
                if m
            ]
            yield (f for _, f in toc_names if f is not None)

            flavorless_toc_names = [n for n, f in toc_names if f is None]
            if flavorless_toc_names:
                tocs = (
                    _parse_toc_file(addon_zip.read(n).decode("utf-8-sig"))
                    for n in flavorless_toc_names
                )
                interface_numbers = (
                    i
                    for t in tocs
                    for s in (t.get("Interface"),)
                    if s
                    for i in _extract_interface_numbers(s)
                )
                yield _interface_numbers_to_flavours(interface_numbers)


async def _parse_repo(get: _Get, repo: Mapping[str, Any]):
    async with get(
        (_API_URL / "repos" / repo["full_name"] / "releases").with_query(per_page=1)
    ) as releases_response:
        releases = await releases_response.json()

    if releases:
        (release,) = releases
        maybe_release_json_asset = next(
            (a for a in release["assets"] if a["name"] == "release.json"), None
        )
        if maybe_release_json_asset is not None:
            async with get(
                maybe_release_json_asset["browser_download_url"]
            ) as release_json_response:
                try:
                    release_json_contents = await release_json_response.json(content_type=None)
                except json.JSONDecodeError:
                    logger.exception(
                        "release.json is not valid JSON:"
                        f" {maybe_release_json_asset['browser_download_url']}"
                    )
                    return

            try:
                release_json = _release_json_converter.structure(
                    release_json_contents, ReleaseJson
                )
            except BaseValidationError:
                logger.exception(
                    "release.json has incorrect schema:"
                    f" {maybe_release_json_asset['browser_download_url']}"
                )
                return

            flavors = frozenset(m.flavor for r in release_json.releases for m in r.metadata)
            release_json_release = release_json.releases[0]
            try:
                project_ids = await _extract_project_ids_from_toc_files(
                    get,
                    next(
                        a["browser_download_url"]
                        for a in release["assets"]
                        if a["name"] == release_json_release.filename
                    ),
                    release_json_release.filename,
                )
            except StopIteration:
                logger.warning(f"release asset does not exist: {release_json_release.filename}")
                return

        else:
            release_archives = [
                a
                for a in release["assets"]
                if a["content_type"] in {"application/zip", "application/x-zip-compressed"}
                and a["name"].endswith(".zip")
            ]
            if not release_archives:
                # Not a downloadable add-on
                return

            flavors = frozenset(
                {
                    f
                    async for a in _extract_game_flavors_from_tocs(get, release_archives)
                    for f in a
                }
            )
            release_archive = release_archives[0]
            project_ids = await _extract_project_ids_from_toc_files(
                get, release_archive["browser_download_url"], release_archive["name"]
            )

        return Project(
            id=repo["id"],
            name=repo["name"],
            full_name=repo["full_name"],
            url=repo["html_url"],
            description=repo["description"],
            last_updated=dt.datetime.fromisoformat(f"{release['published_at'].rstrip('Z')}+00:00"),
            flavors=ProjectFlavors(flavors),
            has_release_json=maybe_release_json_asset is not None,
            last_seen=dt.datetime.now(dt.UTC),
            **project_ids,
        )


@asynccontextmanager
async def _make_http_client(token: str):
    from aiohttp_client_cache import CacheBackend
    from aiohttp_client_cache.session import CachedSession

    from ._http_cache import make_cache

    async with AsyncExitStack() as exit_stack:
        cache_backend = CacheBackend(
            autoclose=False,
            cache_name="http-cache.db",
            expire_after=_CACHE_INDEFINITELY,
            urls_expire_after=_EXPIRE_URLS,
            timeout=20,
        )
        cache_backend.responses, cache_backend.redirects = await exit_stack.enter_async_context(
            make_cache()
        )
        client = await exit_stack.enter_async_context(
            CachedSession(
                cache=cache_backend,
                connector=aiohttp.TCPConnector(limit_per_host=8),
                headers={
                    "Accept": "application/vnd.github.v3+json",
                    "Authorization": f"token {token}",
                    "User-Agent": USER_AGENT,
                },
                timeout=aiohttp.ClientTimeout(sock_connect=10, sock_read=10),
            )
        )

        @asynccontextmanager
        async def get(url: str | URL, **kwargs: Any):
            for _ in range(5):
                async with client.get(url, **kwargs) as response:
                    logger.debug(
                        f"fetching {response.url}"
                        + (
                            f"\n\t{response.headers['X-RateLimit-Remaining'] or '?'} requests"
                            " remaining"
                            if "X-RateLimit-Remaining" in response.headers
                            else ""
                        )
                    )

                    if response.status == 403:
                        logger.error(await response.text())

                        sleep_until_header = response.headers.get("X-RateLimit-Reset")
                        if sleep_until_header:
                            sleep_until = dt.datetime.fromtimestamp(  # noqa: DTZ006
                                int(response.headers["X-RateLimit-Reset"])
                            )
                            sleep_for = max(
                                0,
                                (sleep_until - dt.datetime.now()).total_seconds(),  # noqa: DTZ005
                            )
                            logger.info(
                                "rate limited after"
                                f" {response.headers['X-RateLimit-Used']} requests, sleeping"
                                f" until {sleep_until.time().isoformat()} for"
                                f" ({sleep_for} + 1)s"
                            )
                            await asyncio.sleep(sleep_for + 1)

                    elif response.status != 404 and not response.ok:
                        logger.debug(
                            f"request errored with status {response.status}; sleeping for 3s"
                        )
                        await asyncio.sleep(3)
                        continue

                    else:
                        yield response
                        break
            else:
                response.raise_for_status()  # pyright: ignore[reportPossiblyUnboundVariable]

        yield (client, get)


async def get_projects(
    *,
    token: str,
    existing: Sequence[Project] = (),
    full: bool = False,
    search_params: Sequence[tuple[Literal["code", "repositories", "user_repositories"], str]]
    | None = None,
    since: dt.datetime | None = None,
) -> tuple[set[Project], set[str]]:
    if search_params is None:
        repository_queries = [
            "topic:wow-addon",
            "topic:world-of-warcraft-addon",
            "topic:warcraft-addon",
            "topic:wow-addons",
            "topic:wowaddon",
            "topic:worldofwarcraft",
            "topic:wow",
            "topic:classic-wow",
            "topic:wow-classic",
            "topic:wow-forever",
            "topic:elvui topic:plugin",
            "topic:world-of-warcraft language:lua",
            "topics:>2 topic:world-of-warcraft topic:addon",
        ]
        if full or not existing:
            search_params = [
                ("code", 'extension:toc "## Interface"'),
                ("code", '"## X-Curse-Project-ID" extension:toc'),
                ("code", '"## X-Wago-ID" extension:toc'),
                ("code", '"## X-WoWI-ID" extension:toc'),
                ("code", 'filename:release.json "releases"'),
                ("code", "path:.github/workflows bigwigsmods packager"),
                ("code", "path:.github/workflows CF_API_KEY"),
                *(
                    ("code", f'"## Interface: {interface}" extension:toc')
                    for interface in ("16001", "115", "205", "505", "120")
                ),
                *(("repositories", query) for query in repository_queries),
                *(
                    ("repositories", f"{topic} {stars}")
                    for topic in ("topic:wow", "topic:worldofwarcraft", "topic:elvui")
                    for stars in ("stars:0", "stars:1..5", "stars:6..50", "stars:>50")
                ),
            ]
        else:
            since_date = (since or min(
                project.last_seen for project in existing
            )).astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
            search_params = [
                ("repositories", f"{query} pushed:>={since_date}")
                for query in repository_queries
            ]

    async with _make_http_client(token) as (client, get):
        deduped_repos = {
            r["full_name"]: r
            async for r in _find_addon_repos(get, search_params)
            if not r["full_name"].startswith(REPO_EXCLUDES)
        }
        names = sorted(deduped_repos, key=str.lower)
        graphql = await _graphql_repositories(client, names)
        projects = await _projects_from_graphql(client, get, graphql, existing=existing)
        return projects, set(names)


async def get_projects_to_prune(
    *, token: str, prune_candidates: Iterable[str]
) -> Mapping[str, Project | Literal[False]]:
    async with _make_http_client(token) as (_, get):

        async def get_repo(candidate: str):
            try:
                async with get(_API_URL / "repos" / candidate) as get_response:
                    repo = await get_response.json()
                    return (candidate, (await _parse_repo(get, repo)) or False)
            except aiohttp.ClientResponseError:
                return (candidate, False)

        return {
            f: s
            for c in asyncio.as_completed(get_repo(f) for f in prune_candidates)
            for (f, s) in (await c,)
        }
