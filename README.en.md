# ModPort

[English](README.en.md) | [简体中文](README.md)

ModPort helps migrate Forge mods to NeoForge by organizing source review, migration planning, code changes, target builds and tests, independent review, and runtime evidence. The workflow aims to preserve the behavior requested by the user; completion of a Run by itself does not prove that the behavior has been accepted.

Copyright © 2026 [FlightDan](https://github.com/FlightDan/). Licensed under AGPL-3.0-only.

Version 1.0.0 includes workflow 40. Download the Linux or Windows desktop bundle, Python wheel, or source archive from [Releases](https://github.com/FlightDan/ModPort/releases/tag/v1.0.0). Desktop bundles include Python, OpenCode and Dispatcher SDK; Git, suitable JDKs and GitHub CLI for contributions are separate prerequisites. New Runs use the workflow included with the installed ModPort source.

## Requirements

- Python 3.10 or newer.
- Install `dispatcher-sdk==0.7.1` separately. Its wheel and source distribution are available from the [official v0.7.1 release](https://github.com/FlightDan/dispatcher-sdk/releases/tag/v0.7.1); ModPort does not distribute the SDK through PyPI. A complete workspace keeps the original release assets in `build/sdk-release/0.7.1/` and extracts the source distribution into `dispatcher-sdk/`.
- Git, a JDK and build tools suitable for the source and target versions, access to the required Minecraft/loader dependencies, and credentials for the configured model service.
- OpenCode 1.18.32 for command-line agents. Project builds on Linux run through bubblewrap. The desktop runtime can also use systemd to supervise long-running work. See [Desktop documentation](docs/DESKTOP.md) for platform details and current verification limits.

## Install from source

Create a Python environment, install the separately obtained SDK first, then install the current ModPort source. If the complete workspace contains the SDK release assets, install the local wheel. Otherwise, download the wheel from the [official release](https://github.com/FlightDan/dispatcher-sdk/releases/tag/v0.7.1), or install the source directory extracted from its source distribution:

```sh
python3 -m venv .venv

.venv/bin/python -m pip install --no-deps https://github.com/FlightDan/dispatcher-sdk/releases/download/v0.7.1/dispatcher_sdk-0.7.1-py3-none-any.whl

# Alternatively, install source extracted from the official source distribution.
# .venv/bin/python -m pip install --no-deps ./dispatcher-sdk

.venv/bin/python -m pip install --no-deps -e .
.venv/bin/modport --help
```

The SDK wheel or source directory must be version 0.7.1. `--no-deps` prevents pip from trying to obtain the SDK from a package index; the matching SDK must already be installed before running the command. The standalone ModPort source package and wheel do not include the SDK. The desktop bundle includes the corresponding SDK source and license files from `dispatcher-sdk/`.

Bounded Linux SDK integration checks have passed: imports and `pip check`, 14 SDK compatibility tests, and checks for reopening executions, ACK/fencing, receipt recovery across a commit gap, and process-supervisor cancellation and recovery. Full real Hyperbox acceptance and native Windows acceptance remain incomplete. These checks do not establish full migration acceptance. Installing ModPort from source also requires the build tools declared in `pyproject.toml`; prepare those tools first for offline installation. See the [documentation index](docs/README.md) for the directory layout.

Before using the command line, install the pinned OpenCode version and configure the selected model service through its authentication flow:

```sh
npm install -g opencode-ai@1.18.32
opencode --version
```

You can also set `MODPORT_OPENCODE_BIN` to the executable for that version. ModPort does not read Codex login data. Linux client tests also require Xvfb; the required JDK and build dependencies depend on the locked target version.

## Start a migration

Provide the source repository and revision, along with the Minecraft, loader, and Java versions that the mod actually uses. The following version arguments are placeholders; replace them with versions supported by the project and build environment.

```sh
.venv/bin/modport run \
  --mod-id examplemod \
  --source-repository "$SOURCE_REPOSITORY_URL" \
  --source-revision COMMIT_OR_TAG \
  --source-minecraft SOURCE_MINECRAFT_VERSION \
  --source-loader forge \
  --source-loader-version SOURCE_FORGE_VERSION \
  --source-java SOURCE_JAVA_VERSION \
  --target-minecraft TARGET_MINECRAFT_VERSION \
  --target-loader neoforge \
  --target-loader-version TARGET_NEOFORGE_VERSION \
  --target-java TARGET_JAVA_VERSION
```

Use `--max-seconds`, `--max-agent-assignments`, and `--max-parallel-coders` to adjust the Run limits. `--validation-scope compile_package` defers runtime and game-behavior tests, so acceptance remains `unverified`. The default scope is `full`.

Each Run records its inputs, workflow definition, and selected model settings. Use `status`, `resume`, `cancel`, and `recover` with a Run directory and ID to inspect or manage a Run; `modport <command> --help` shows the current arguments. Runs created with another workflow or SDK version may not be compatible with the current installation.

## Data directories

By default, ModPort stores application data in the current user's data directory:

- Linux: `$XDG_DATA_HOME/modport` when `XDG_DATA_HOME` is an absolute path; otherwise `~/.local/share/modport`.
- Windows: `%LOCALAPPDATA%\ModPort`; if `LOCALAPPDATA` is unset, `%USERPROFILE%\AppData\Local\ModPort`.
- macOS: `~/Library/Application Support/ModPort`.

Set `MODPORT_DATA_ROOT` to use another application data directory. Runs are stored under its `runs/` subdirectory by default; set `MODPORT_OUTPUT_ROOT` to override the Run directory separately. Reusable migration skills are stored in `migration-skills/` by default and can be moved with `MODPORT_SKILL_STORE`. Archived Run artifacts are stored in `archives/` by default and can be moved with `MODPORT_ARCHIVE_ROOT`. Keep these directories private because they may contain source code, migration changes, logs, and evidence.

The desktop application uses Electron's user data directory; `MODPORT_DESKTOP_DATA_ROOT` can override it. New instances save the selected skill library and driver path, and recovery continues with those selections. Releasing archived data requires the archive directory to be on a separate filesystem. Set `MODPORT_ARCHIVE_ROOT` explicitly to enable release; the default location is not guaranteed to meet this requirement. Older local data was moved to the ignored `legacy/` directory and is not imported into new Runs.

## Model settings

Model selection is independent of the workflow version. Inspect the effective settings or change a model by role without editing Python files:

```sh
.venv/bin/modport models show
.venv/bin/modport models set --role planner --model PROVIDER/MODEL --reasoning-effort EFFORT
.venv/bin/modport models show --config /path/to/modport-models.json
```

Supported roles are `default`, `planner`, `coder`, `supervisor`, `contract_review`, and `summary`. Entries for individual stages in the JSON configuration can override role settings. Configuration is read in this order: `MODPORT_MODEL_CONFIG`, `modport-models.json` in the current directory, then the package defaults. The selected settings are frozen when a Run is submitted; later changes affect only new Runs. Configure model-service credentials in the runtime environment and do not put them in the source repository or migration inputs.

## Behavior verification

ModPort records behavior requirements from source code and documentation, then designs target tests independently and maps their results back to those requirements. Source test harnesses do not count as target acceptance evidence. Every required target case must actually execute and pass before target behavior can be reported as accepted. Skipped, missing, deferred, or failed cases leave acceptance unverified or mark the Run failed. A successful build or SDK execution status alone does not prove that behavior has been accepted.

The [workflow guide](docs/WORKFLOW.md) describes migration stages and the evidence model.

## Desktop application

The command-line and desktop interfaces support Simplified Chinese and English. Use the language selector in the top-right corner of the desktop app to switch and save the language. The command line accepts `modport --lang zh-CN --help` or `modport --lang en --help`. On first use, the app follows the system language; unsupported languages fall back to English. Language settings do not change the migration workflow.

The desktop app provides project settings, model configuration, execution status, and Supervisor chat. Migration still requires Git, a suitable JDK, model access, and supported build tools for the target platform. See [Desktop documentation](docs/DESKTOP.md) for current behavior and limitations. Native Windows startup, recovery, sandbox permissions, and file-lock behavior remain unverified. Full migration acceptance remains unverified.

## Community Wiki research and contributions

The `modport wiki` CLI provides `update`, `import-pack`, `build-pack`, `drafts`, `export`, and `export-draft` commands to update local Wiki content for new migration instances, import or build research packs, list drafts, and export Run findings or draft files. Use `modport wiki --help` to see their arguments. The desktop **Research contributions** view supports reviewing and editing local drafts, browser-based GitHub sign-in, Draft PR submission, and updating the local Wiki. Submitting a Draft PR requires GitHub CLI (`gh`). Actual browser authorization, owner-account Draft PR submission and same-PR submission replay were verified. The first [research-v0.1.0 package](https://github.com/FlightDan/modport-wiki-for-agents/releases/tag/research-v0.1.0) was published and its anonymous download and offline import were verified. A real model assignment read that published material from the saved cache through the production SDK/MCP path, preserved its citations and automatically exported an editable contribution draft. This was documentation-only research; full migration acceptance, external contributor fork routing and native Windows behavior remain unverified. This integration is included in ModPort 1.0.0.

Wiki material is optional research context and cannot by itself establish that a project's migration passed acceptance. See the [Wiki knowledge and integration guide](docs/WIKI_KNOWLEDGE.md).

## Read-only progress page

The optional `web` command serves a password-protected, read-only page for viewing saved Runs. Bind it to loopback for local access:

```sh
.venv/bin/modport web --host 127.0.0.1 \
  --password-file /path/to/password-file
```

The page does not start, cancel, or retry migrations and does not call a model. Default HTTP transport is unencrypted; do not expose it to an untrusted network. For remote access, use a trusted reverse proxy or SSH tunnel.

## More documentation

- [Workflow guide](docs/WORKFLOW.md): migration stages, Run inputs, and recovery semantics.
- [Agent rules](docs/AGENT_RULES.md): constraints for migration participants.
- [Run evidence protocol](docs/EVIDENCE_PROTOCOL.md): evidence structures and acceptance boundaries.
- [Desktop documentation](docs/DESKTOP.md) and [desktop API](docs/DESKTOP_API.md): desktop behavior and local interfaces.
- [Driver lease](docs/DRIVER_LEASE.md): driver ownership and recovery for a continuing Run.
- [Release guide](docs/RELEASING.md): source, SDK integration, and desktop packaging boundaries.
- [Wiki knowledge and integration](docs/WIKI_KNOWLEDGE.md): optional version-specific research and contribution flow.

## License and migration outputs

ModPort is licensed under **AGPL-3.0-only**. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists third-party components and their terms. See the full [license](LICENSE) and [attribution notice](ATTRIBUTION.md).

ModPort's license does not automatically apply to migrated or modified mods or other output files. Their licensing depends on the relevant source, dependencies, and project licenses. The acknowledgment in `ATTRIBUTION.md` is voluntary. If you want to credit the tool, you can write: **“This project was migrated using ModPort.”**

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution terms and [SECURITY.md](SECURITY.md) for private vulnerability-reporting guidance.
