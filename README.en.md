# ModPort

[![Language: Simplified Chinese / English](https://img.shields.io/badge/Language-简体中文%20%2F%20English-blue)](#getting-started)
[![Desktop download: Windows](https://img.shields.io/badge/Desktop-Windows-0078D4)](https://github.com/FlightDan/ModPort/releases)
[![Desktop download: Linux](https://img.shields.io/badge/Desktop-Linux-FCC624?logo=linux&logoColor=black)](https://github.com/FlightDan/ModPort/releases)
[![License: AGPL-3.0-only](https://img.shields.io/badge/License-AGPL--3.0--only-green)](LICENSE)

[简体中文](README.md) | [English](README.en.md)

**Tired of maintaining multiple versions? Keep your focus on one main branch. Give ModPort a try.**

When Minecraft updates, your mod may need to move too. That means researching API changes, updating code, fixing build errors, and checking in-game behavior. ModPort helps you work through those recurring steps so you can spend more time on the features you want to build.

ModPort is an AI-agent-powered tool for migrating Minecraft mods from Forge to NeoForge. It organizes the full migration process into fine-grained tasks, parallel work, and layered oversight to make automated migrations easier to manage and use model budgets thoughtfully.

- **Move independent work forward in parallel.** Split a migration into clear tasks so agents can research and update separate areas at the same time, then connect the work through dependencies, integration, and verification.
- **Choose a model for each kind of work.** Use a more capable model for planning and complex decisions, and a more economical model for well-scoped execution. Configure models and reasoning effort by role and stage to focus your budget where it matters.
- **Coordinate work and follow its progress.** Dispatcher SDK manages task dependencies, execution budgets, and recovery, with independent review and supervisor agents to reduce the effort of tracking handoffs and handling interruptions.

**Less time maintaining versions, more time creating.**

[Download ModPort](https://github.com/FlightDan/ModPort/releases) · [See a migration case](#real-migration-case) · [Documentation](docs/README.md) · [Feedback and suggestions](https://github.com/FlightDan/ModPort/issues)

## Getting started

### Desktop: download, configure, and start a migration

Windows and Linux users can download the desktop bundle for their platform from **[GitHub Releases](https://github.com/FlightDan/ModPort/releases)**.

| Platform | How to start |
| --- | --- |
| Windows | Extract the package to a local folder and run `ModPort.exe` |
| Linux | Extract the package to a local folder and run `./ModPort` |

The desktop bundle includes Python, OpenCode, and Dispatcher SDK. You will also need **Git, a JDK suitable for your project version, and access to a model service**. Linux project execution requires bubblewrap; client tests also require Xvfb. The environment check will point out missing dependencies.

1. **Configure a model.** Open “Configure Models” in the top-right corner and enter the API address, key, and model. You can choose separate models for complex tasks and routine coding, or use one model for both.
2. **Choose a project.** Provide the source repository and a branch, tag, or commit, or choose a local source directory. Confirm the source and target versions, then choose a development workspace.
3. **Set a budget and start the migration.** Follow task progress, model usage, and Supervisor chat on the execution page. Open the working directory to inspect source changes and build results.

The interface supports Simplified Chinese and English, and you can switch languages at any time from the top-right corner. The desktop app supervises migrations through systemd on Linux or Task Scheduler on Windows. Once a background task has started successfully, closing the window does not cancel it; reopen the app to check its progress. See the [desktop documentation](docs/DESKTOP.md) for environment requirements.

Platform dependencies, background execution, and current verification limits are described in the [desktop documentation](docs/DESKTOP.md). A Windows package is available, while native startup, recovery, and sandbox behavior still need full verification. macOS testing is on the roadmap below.

### CLI: start from a terminal

The CLI is suited to terminal users, remote development, and scripts, and it also supports English and Simplified Chinese. First install **Python 3.10+, Node.js/npm, Git, and a JDK suitable for your project**, then follow the instructions for your platform.

Install Dispatcher SDK `0.7.1` separately from its official Release. The commands below do not rely on the SDK being available from PyPI.

<details>
<summary><strong>Linux installation</strong></summary>

```bash
git clone https://github.com/FlightDan/ModPort.git
cd ModPort
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip "setuptools>=77" wheel
python -m pip install --no-deps https://github.com/FlightDan/dispatcher-sdk/releases/download/v0.7.1/dispatcher_sdk-0.7.1-py3-none-any.whl
python -m pip install --no-deps -e .
python -m pip check

npm install -g opencode-ai@1.18.32
opencode auth login
modport --lang en --help
```

Linux project execution also requires bubblewrap. Install Xvfb if you need to run client tests. The exact installation steps depend on your Linux distribution.

</details>

<details>
<summary><strong>Windows installation (PowerShell)</strong></summary>

```powershell
git clone https://github.com/FlightDan/ModPort.git
cd ModPort
py -3 -m venv .venv

.\.venv\Scripts\python.exe -m pip install --upgrade pip "setuptools>=77" wheel
.\.venv\Scripts\python.exe -m pip install --no-deps https://github.com/FlightDan/dispatcher-sdk/releases/download/v0.7.1/dispatcher_sdk-0.7.1-py3-none-any.whl
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pip check

npm install -g opencode-ai@1.18.32
opencode auth login
.\.venv\Scripts\modport.exe --lang en --help
```

For the `modport` commands below, use `.\.venv\Scripts\modport.exe` on Windows. Convert multiline Bash examples to a single line or use PowerShell backticks for line continuation.

</details>

After configuring your model service through OpenCode, inspect or adjust ModPort's model settings:

```bash
modport models show
modport models set --role planner --model PROVIDER/MODEL --reasoning-effort EFFORT
modport models set --role coder --model PROVIDER/MODEL --reasoning-effort EFFORT
```

Replace `PROVIDER/MODEL` and `EFFORT` with model identifiers and reasoning levels supported by your service. Model settings are saved when a migration starts; later changes apply to new migrations.

**CLI preview** (English help output):

```text
$ modport --lang en status --help
usage: modport status [-h] [--lang {en,zh-CN}] --run-dir RUN_DIR --run-id
                      RUN_ID [--detail | --task-id TASK_ID]

options:
  -h, --help         show this help message and exit
  --lang {en,zh-CN}  interface language (en or zh-CN); manual selection is remembered
  --run-dir RUN_DIR
  --run-id RUN_ID
  --detail           read the full Run snapshot; may require a bounded database backup
  --task-id TASK_ID  read the latest SDK attempt for one task without expanding the whole Run
```

<details>
<summary><strong>Start a migration</strong></summary>

This is a Bash argument template. Replace the repository, revision, and version placeholders with values for your project:

```bash
modport run \
  --mod-id examplemod \
  --source-repository https://github.com/YOUR_NAME/YOUR_MOD.git \
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

Use `--max-seconds` to set the total time limit, `--max-agent-assignments` to set the agent assignment limit, and `--max-parallel-coders` to set the number of coding tasks that may run in parallel. The default verification scope is `full`. Choosing `--validation-scope compile_package` only builds the package; game behavior remains unverified.

Use the Run directory and ID returned at startup to check its status:

```bash
modport status --run-dir /path/to/run --run-id RUN_ID
modport status --run-dir /path/to/run --run-id RUN_ID --detail
```

Use `resume`, `cancel`, and `recover` to continue, cancel, or recover a Run. See `modport <command> --help` for the arguments. To change the interface language, use `--lang en` or `--lang zh-CN`.

</details>

## Real migration case

### ScalingHealth → NeoForge

**[ScalingHealth-NeoForge](https://github.com/ModPortMC/ScalingHealth-NeoForge)** is a real migration case completed with ModPort. Browse the migrated source to see what a mod project can look like after moving to NeoForge.

👉 **[Explore the ScalingHealth migration](https://github.com/ModPortMC/ScalingHealth-NeoForge)**

If you have used ModPort for a migration, you are welcome to share it here so more familiar mods can appear.

## Parallel workflow and layered oversight

A migration involves version research, dependency adaptation, code changes, and behavior verification. Some tasks are independent while others depend on earlier work. ModPort organizes these relationships into an executable workflow so agents can move forward within clearly defined scopes.

**[Dispatcher SDK](https://github.com/FlightDan/dispatcher-sdk)** manages execution and scheduling. ModPort handles the migration process, task context, and artifact handoffs; OpenCode runs the agent tasks. Each coding task has its own agent, which can use subagents within its permissions and budget.

| Area | What it does |
| --- | --- |
| Research and planning | Read source code and version references, identify behavior requirements, and break the migration into dependent tasks |
| Parallel coding | Make changes in separate workspaces and deliver work for integration |
| Integration and verification | Integrate changes, clean up code, build the project, and run tests on the target version |
| Independent review and supervision | Review changes, investigate failures or stalled work, and request repairs or recovery through the workflow |

The design aims to address three things: **less serial waiting, fewer manual handoffs, and managed model costs.** You can configure models and reasoning effort for planning, coding, supervision, review, and summaries. Use more capability for complex decisions and economical models for well-scoped tasks. Actual costs depend on project complexity, model pricing, and the number of repair attempts.

Budgets, progress, and recovery records are maintained throughout execution. Background supervision observes model responses and substantive progress, and routes issues through diagnosis and recovery while retaining the original time limit and cumulative budget. See the [workflow guide](docs/WORKFLOW.md) for details.

## More useful features

- **Local source and separate workspaces.** Start from a remote repository or a local directory. Choose a Git worktree, a directory copy, or direct editing in the original directory after explicit confirmation. Local source is also sent to the configured model service.
- **Reusable migration knowledge.** Use migration rules, skills, and community Wiki research packs to provide version-specific context for new tasks. You can review research drafts and contribute them to the community. See [Wiki knowledge and contributions](docs/WIKI_KNOWLEDGE.md).
- **Conflict repair and recovery.** When integration encounters an actual conflict, an agent works on it in an isolated workspace before returning to the original integration stage. Recovery after a driver or supervisor crash retains the original deadline and cumulative budget; the execution page shows scheduling state and observed process state separately.
- **Build and test results you can review.** Map target tests back to source behavior requirements and distinguish a successful build from verified behavior. Required cases must actually run and pass; skipped or missing cases do not count as passed.
- **A read-only progress page.** Use `modport web` to view saved Runs remotely. See the details below for configuration.

<details>
<summary><strong>Data locations and progress page</strong></summary>

By default, the CLI stores data in the current user's application data directory:

| Platform | Default directory |
| --- | --- |
| Linux | `$XDG_DATA_HOME/modport` when the variable is an absolute path; otherwise `~/.local/share/modport` |
| Windows | `%LOCALAPPDATA%\ModPort`; if unset, `%USERPROFILE%\AppData\Local\ModPort` |
| macOS | `~/Library/Application Support/ModPort` (platform testing is pending) |

`MODPORT_DATA_ROOT` overrides the application data directory. `MODPORT_OUTPUT_ROOT`, `MODPORT_SKILL_STORE`, and `MODPORT_ARCHIVE_ROOT` override the Run, migration skill, and archive directories, respectively. The desktop app uses Electron's user data directory; set `MODPORT_DESKTOP_DATA_ROOT` to override it.

Run data may contain source code, logs, and migration results, so keep it in a private directory. To archive data and free space, configure the archive directory on a separate filesystem; the default location is not guaranteed to meet this requirement.

Example: bind the read-only page to the local machine address:

```bash
modport web --host 127.0.0.1 --password-file /path/to/password-file
```

The page does not start, cancel, or retry migrations and does not call a model. For remote access, use a trusted reverse proxy or SSH tunnel instead of exposing unencrypted HTTP to an untrusted network.

</details>

Learn more: [Desktop documentation](docs/DESKTOP.md) · [Workflow](docs/WORKFLOW.md) · [Agent rules](docs/AGENT_RULES.md) · [Run evidence](docs/EVIDENCE_PROTOCOL.md) · [Documentation index](docs/README.md)

## Roadmap

- [ ] **macOS testing:** verify installation, startup, migration execution, and recovery.
- [ ] **Broader automatic conflict repair:** cover more complex conflict scenarios and further reduce manual intervention.

If you are a mod developer and have a version, migration scenario, or feature you would like to see supported, please get in touch through **[GitHub Issues](https://github.com/FlightDan/ModPort/issues)**. You are also welcome to submit a PR and contribute code, documentation, or migration knowledge.

See the [contribution guide](CONTRIBUTING.md) for ways to contribute. For private vulnerability reports, see [security guidance](SECURITY.md).

## License

Copyright © 2026 [FlightDan](https://github.com/FlightDan/). ModPort is licensed under **[AGPL-3.0-only](LICENSE)**. Third-party components and their terms are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

ModPort's license does not automatically apply to migrated or modified mods or other files. Their licensing depends on the relevant source, dependencies, and project licenses. See the [attribution notice](ATTRIBUTION.md) for details.

If you like, you can add this acknowledgment to a migrated project: **“This project was migrated using ModPort.”**
