# Third-party notices

ModPort's AGPL-3.0-only declaration applies to the project as a whole, subject
to the licenses and notices of the separately identified material below.
It does not replace those licenses or claim ownership of third-party work.

## Bundled source material

| Component | Location | Upstream | License |
| --- | --- | --- | --- |
| Superpowers debugging and verification skills | `src/modport/vendor_skills/superpowers/` | https://github.com/obra/superpowers | MIT; Copyright (c) 2025 Jesse Vincent |
| ModPort adaptation of Code Simplifier | `src/modport/vendor_skills/code-simplifier/` | https://github.com/anthropics/claude-plugins-official/tree/main/plugins/code-simplifier | Apache-2.0; accompanying `LICENSE` and `NOTICE` |

The Superpowers license and existing upstream revision record remain alongside
the bundled files. Code Simplifier has been adapted for ModPort's migration
scope and host execution boundaries; its upstream Apache-2.0 text is preserved
alongside the adaptation. Upstream license locations were checked on 2026-10-06.

## Runtime dependencies

`dispatcher-sdk==0.7.1` is a separate Apache-2.0 project. Its published
[v0.7.1 release](https://github.com/FlightDan/dispatcher-sdk/releases/tag/v0.7.1)
provides a wheel and source distribution; the standalone ModPort source
distribution does not embed the SDK. The release's notice reads:

> Agent Dispatcher SDK\
> Copyright 2026 The Agent Dispatcher Authors\
> This product includes software developed by the Agent Dispatcher contributors.

The Python wheel declares the dependency; it does not embed the SDK. The
workspace keeps the original release assets under `build/sdk-release/0.7.1/`
and the sdist source under `dispatcher-sdk/`. Desktop bundles include that SDK
source and retain its `LICENSE` and `NOTICE` in `licenses/`. Bounded Linux
integration checks for SDK 0.7.1 have passed, while full real Hyperbox and native
Windows acceptance remain unverified. See [docs/RELEASING.md](docs/RELEASING.md)
for the checked paths and their limits; SDK publication alone does not establish
migration acceptance.

## Desktop runtimes

Desktop bundles additionally contain Electron, Python and OpenCode, each under
its own upstream terms. Preserve the license and third-party notice files
shipped in their official runtime archives. The desktop assembly script also
copies the supplied OpenCode license into `licenses/OpenCode-LICENSE.txt`.
The complete runtime archives, including their notices, must accompany a
binary distribution. ModPort's license does not relicense these runtimes.

Runtime download inputs and publication requirements are documented in
[docs/RELEASING.md](docs/RELEASING.md). A source-only ModPort distribution does
not include those runtime binaries or the SDK checkout.

## Migrated projects and downloaded tools

Minecraft, Forge, NeoForge, Fabric, mods, JDKs, Gradle distributions and other
downloaded dependencies are not covered by ModPort's license. Their own terms
apply. Local migration instances, source archives, caches, credentials and
historical validation records are not part of the public source distribution.
