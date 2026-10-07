# Migration knowledge and contributions

ModPort uses [modport-wiki-for-agents](https://github.com/FlightDan/modport-wiki-for-agents) as optional, version-specific research. The current local implementation supports saved Wiki references, offline research packs, editable contribution drafts and GitHub CLI sign-in. It keeps the existing Electron application and workflow stages.

This implementation has passed focused host, SDK, MCP and browser checks. Actual GitHub CLI browser authorization, owner-account Draft PR submission, same-PR submission replay and account/receipt persistence across an application service restart were verified with [contribution #1](https://github.com/FlightDan/modport-wiki-for-agents/pull/1). That reviewed contribution was merged and promoted into the [research-v0.1.0 package](https://github.com/FlightDan/modport-wiki-for-agents/releases/tag/research-v0.1.0). The real anonymous reader selected that release and downloaded its index and body using the public Git fallback during REST rate exhaustion; its public ZIP was also downloaded and imported. A real documentation-research assignment consumed the published cache through production SDK/MCP tools, preserved its knowledge revision and primary-source citations, and automatically exported an editable draft. This single-stage execution does not establish full migration acceptance. External contributor fork routing and native Windows credential/process behavior remain unverified. The application integration is included in ModPort 1.0.0.

## Read and cite

The host selects material only when both versions are known. Platform identities contain exactly `minecraft`, `loader` and `loader_version` on both sides. Java identities contain exactly `java`. Adjacent versions and an unknown loader version do not count as matches.

The first retrieval selects the latest GitHub release tag, or `main` when there is no release, and copies the repository revision returned by GitHub. The host saves the catalogue, selected pages, revision, retrieval time and primary source URLs under the Run's `artifacts/wiki/`. Planning, execution and independent review use these saved materials. Updating the local cache affects later Runs; it does not replace an existing Run's selection.

If REST metadata requests return 403 or 429, the host uses the public latest-release redirect and Git reference advertisement to resolve the same selection without contributor credentials or another REST retry. It records the original API observation and the metadata transport. Missing branches/tags are reported explicitly. A discovered revision with no `index.json` is reported as an unpublished research index, rather than mislabeled as an authentication problem.

Agents read path references through `modport_sandbox_read_run_artifact`. Wiki pages are data, never execution instructions or project acceptance evidence. Reuse applicable findings, investigate missing or disputed claims against official versioned documentation or locked source, and cite the knowledge revision, page/entry and primary source locator in the normal research report. An unavailable API, missing page, mismatched version or incomplete selection is diagnostic; ordinary research continues.

CLI migration and standalone skill requests accept `--wiki-revision REVISION` and `--no-wiki`. Omit both to use supplementary research by default. Retrieval is bounded by the active SDK workload window and uses an unauthenticated downloader with a total process timeout. It never uses the contributor's GitHub credentials. Network-disabled assignments can use the saved local research cache.

## Review and submit a draft

New portable findings recorded by research agents are automatically exported after the assignment. Approved generic findings from independent gap review are also exported. A missing or invalid optional contribution file produces a local diagnostic, without failing the migration. Re-exporting the same finding origin preserves user edits.

1. Open **研究贡献 / Research contributions** in the desktop header, or use the current Run's contribution button to recover its local exports.
2. Select a draft and review its exact version pair, editable research JSON, PR title and optional notes. Save changes before submitting.
3. Select **登录 GitHub / Sign in to GitHub**. GitHub CLI displays a device code and opens the official browser login. Confirm the account shown after login.
4. Select **提交 Draft PR / Submit Draft PR**. The host submits only the portable contribution files using that account's fork and branch. The upstream owner uses an owned branch. The draft remains locally available after a failure; retry resumes the recorded submission rather than deliberately creating a second PR.

Starting submission freezes that draft's content and account, including when a later request fails. Retry preserves that transaction. To revise a finding after submission starts, export it and prepare a separate reviewed manual PR; this first UI does not provide a duplicate-draft action. Review and save before submitting.

GitHub CLI must be installed and available on the desktop host. The first version does not bundle it or implement a separate OAuth app. Authentication uses an application-specific GitHub CLI configuration. The system credential store may share account slots with other CLI installations; the host checks the configured and observed account before submission. If a credential store is unavailable, GitHub CLI can store credentials in the private authentication directory. The UI describes this behavior; it does not guarantee keyring storage.

Authentication, private finding origins and submission receipts remain outside Run inputs and exported drafts. No authentication or upload tool is given to a research agent. Exported files exclude credentials, private project paths, Run/task identifiers and project acceptance results. These filters are a practical guard, so users still review the draft before explicitly submitting it.

## Local CLI tools

```sh
modport wiki drafts
modport wiki export --run-dir /path/to/run
modport wiki export-draft --id UUID --output /path/to/research-draft.json
modport wiki update
modport wiki update --revision research-v0.1.0
modport wiki import-pack --file /path/to/research-v0.1.0.zip
```

The desktop's **更新本地 Wiki / Update local Wiki** button performs the same download as `wiki update`. A complete update saves the catalogue and research bodies; a failed update preserves the previous cache. Importing a research pack selects the latest local cache. New online Runs still try the selected remote revision first and use the cache when retrieval is unavailable or host network tools are disabled. There is no user-facing offline-only flag in this first version. GitHub login and assisted submission are desktop actions.

## Maintainer promotion and research packs

The public repository's `index.json` has `schema_version: 1` and an `entries` array. Each row contains `id`, `kind`, `source`, `target` and `path`. Contribution files live at `contributions/{kind}/{uuid}.json`; maintained research files can also live under `platform/` or `java/`. Each file has exactly `schema_version`, `id`, `kind`, `source`, `target` and `entries`.

Each generic entry uses `id`, `category`, `summary`, `applicability`, `migration`, `compat`, `verification` and `evidence`. Evidence rows contain exactly a public HTTP(S) `source`, a precise `locator` and the claim it `supports`. Distinguish source reading, compilation and runtime observations in the text, and retain uncertainties. None of these claims proves a particular ModPort Run passed acceptance.

After reviewing and merging contributions, a maintainer can build a local release asset and the index for promotion:

```sh
modport wiki build-pack --library /path/to/wiki-checkout \
  --revision research-v0.1.0 --output /path/to/research-v0.1.0.zip \
  --index-output /path/to/wiki-checkout/index.json
```

This explicitly indexes the JSON research files in the supplied checkout, validates their portable fields, and writes a ZIP containing the index, revision metadata and declared research files. Use a checkout containing only material selected for promotion. The output ZIP must not already exist. Without `--index-output`, the repository index is left alone.

Review the generated index, commit it, tag the corresponding revision and attach the ZIP to a GitHub release through the normal maintainer process. Those publishing actions are separate from the build command and require the maintainer's authorization. Subsequent online updates resolve the release tag and download indexed files; offline users import the release ZIP. Merging a contribution alone does not promote it, and the tool does not automatically publish or configure CI.

The first published catalogue contains one reviewed Java 17 → 25 finding with version-specific Oracle documentation references and explicit runtime uncertainty. Licensing and contribution terms must be decided by the repository owner; this integration does not add or change a license.
