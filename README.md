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

## Adoption

Derivatives of this catalogue are used by the following add-on managers:

* [instawow](https://github.com/layday/instawow)
* [strongbox](https://github.com/ogri-la/strongbox)
  (via [ogri-la/github-wow-addon-catalogue-go](https://github.com/ogri-la/github-wow-addon-catalogue-go))
* [CurseBreaker](https://github.com/AcidWeb/CurseBreaker)
