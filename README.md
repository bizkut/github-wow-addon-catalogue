# Forever github-wow-addon-catalogue

Forever Launcher's GPL-3.0 add-on catalogue, derived from
[layday/github-wow-addon-catalogue](https://github.com/layday/github-wow-addon-catalogue).
It keeps the upstream CSV columns and adds optional release-stream, asset-name
and folder columns used by Forever's AddonForge build.

## Usage

Requires Python 3.12 or later. Set `RELEASE_JSON_ADDONS_GITHUB_TOKEN`, then:

```sh
uv run python -m cataloguer collect --merge
```

The default is an incremental refresh from `catalogue-state.json`. Pass
`--full` for all discovery queries. GitHub Actions runs the incremental refresh
daily and also supports manual full runs. Discovery validates release ZIPs by
reading their central directory and top-level TOCs; generic tag-stream and
distinct multi-ZIP releases become separate installable rows.
Release discovery follows every page of a repository's release history, so older
independent tag streams remain installable even in repositories with over 100
releases. When GitHub requires smaller query pages, that repository keeps its
successful page size for subsequent pages.

An unavailable ZIP inspection is not classified as a non-add-on. Successfully
inspected folder groups remain available and keep matching last-known package
identities within the same release stream. An unavailable normal ZIP and a
successful `nolib` variant must not produce duplicate packages; cached rows are
retained only when no fresh row covers that folder group in that stream.
Without an established identity, a failed complete package must not turn an
optional recorder into the repository's base add-on.

## Polestar complete package

Verified against the author's [v0.2.6 release](https://github.com/fitzforge/polestar/releases/tag/v0.2.6)
on 2026-10-23. The catalogue previously contained only the optional Recorder asset,
incorrectly named `polestar`. The author already publishes a complete package;
no CurseForge API, unofficial mirror, dependency downloader or repack is needed.

All rows below are in the single author asset
[`Polestar-0.2.6.zip`](https://github.com/fitzforge/polestar/releases/download/v0.2.6/Polestar-0.2.6.zip)
(38,395,132 bytes; SHA-256
`4c2775471a23041359df3d4e74b0c09f509821355ba3ca8968f84b87b514a0ab`).
Every TOC reports version `0.2.6` and interface `16001` (Forever), not Retail,
Wrath, Classic Era or TBC. Every module below depends on `Polestar`.

| Component | Archive folder and matching TOC basename | Packaging |
| --- | --- | --- |
| Core | `Polestar` | Complete package root |
| Codex | `Polestar_Codex` | Load on demand |
| Guides | `Polestar_Guides` | English/base guides, both factions |
| Guides (ptBR) | `Polestar_Guides_ptBR` | Brazilian Portuguese guide texts, load on demand |
| Item data | `Polestar_ItemDB` | Load on demand |
| Names (ptBR) | `Polestar_Names_ptBR` | Brazilian Portuguese game names, load on demand |
| Navigation | `Polestar_Nav` | Load on demand |
| Quest database | `Polestar_QuestDB` | Load on demand |

The complete asset contains 24 add-on folders: these eight plus Guides and Names
for `deDE`, `esES`, `esMX`, `frFR`, `koKR`, `ruRU`, `zhCN` and `zhTW`.
Languages are bundled by the author, not interchangeable release streams; the
locale modules are loaded by the add-on as required. There are no separate
public release assets for the seven requested modules, so listing each as a
separate independently downloaded add-on would misrepresent the upstream package.

The complete catalogue row is `1417882268:polestar-zip`, named **Polestar**, with
asset selector `Polestar-*.zip`, no tag prefix, and all 24 actual archive folders.
It updates as one package. The optional
[`Polestar_Recorder-0.2.6.zip`](https://github.com/fitzforge/polestar/releases/download/v0.2.6/Polestar_Recorder-0.2.6.zip)
is a distinct row, `1417882268:polestar-recorder-zip`, named **Polestar Recorder**,
selector `Polestar_Recorder-*.zip`, folder `Polestar_Recorder` (59,341 bytes;
SHA-256 `d08dbfd375127a5e075d0df6faee1ef30c3b00eca6a1bc1f7dc026198304fe4a`).
It is not a dependency of the complete package.

The packaged TOCs declare GPL-3.0-only, except navigation's GPL-3.0-or-later.
`Polestar/LICENSE` and `Polestar_Nav/LICENSE` carry those notices. The data
folders also carry their own `LICENSE-DATA.txt` attribution, including MIT data
and Blizzard game data; this catalogue links the unchanged author ZIP and does
not relabel all included third-party data as GPL. Exact TOCs and archive member
paths from the hash-verified complete ZIP are recorded in
`tests/fixtures/polestar-0.2.6.json`.

Consumer integration: replace the bundled source CSV with this `addons.csv`.
Use the complete row for an optional starter-pack bundle, with consumer ID
`gh-1417882268:polestar-zip`; do not add the Recorder or invent child catalogue
IDs. Folder reconciliation must map existing `Polestar` and each bundled module
to that same complete row, preserving its selector for future updates.

Focused offline regression contracts:

```sh
uv run python -m unittest discover -s tests -p test_complete_packages.py -v
```

## Adoption

Derivatives of this catalogue are used by the following add-on managers:

* [instawow](https://github.com/layday/instawow)
* [strongbox](https://github.com/ogri-la/strongbox)
  (via [ogri-la/github-wow-addon-catalogue-go](https://github.com/ogri-la/github-wow-addon-catalogue-go))
* [CurseBreaker](https://github.com/AcidWeb/CurseBreaker)
