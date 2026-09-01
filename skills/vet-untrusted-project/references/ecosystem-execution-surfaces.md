# Ecosystem execution surfaces

Read only sections matching detected files and tools. Use this reference to locate target-controlled code that an agent, IDE, install, build, test, or run command can execute early. The purpose is to detect a weaponized repository or project folder, not to audit application security, code quality, privacy, architecture, or domain behavior.

For each detected ecosystem:

1. Identify automatic/configuration-time execution and the first target-controlled files reached by common commands.
2. Search the ecosystem's source and tests for high-signal hostile-repository patterns.
3. Trace deeper only when a lead passes the evidence gate in `SKILL.md`.
4. Stop when behavior is expected, non-deceptive, and unrelated to a hostile-repository effect.

Do not inspect adjacent features after a lead is explained. A prior audit concern, unknown provenance, generated artifact, remote feature, or ordinary risky capability is not a lead without current evidence of reachable weaponized behavior.

Do not recursively search summarized dependency/build trees for nested agent instructions or configuration. Inspect a nested `.agents`, `.claude`, `.codex`, MCP, IDE, or hook path only after independently known loader behavior and current root configuration prove that the exact location will be loaded. Do not inspect endpoint authentication, authorization, exposure, or other application-security properties unless a named weaponization chain depends on them.

Never invoke build, install, test, lint, format, documentation, metadata, IDE-import, or task-listing operations during the audit.

## JavaScript and TypeScript

### npm, Yarn, and pnpm

Inspect:

- `package.json` scripts, especially lifecycle names, `prepare`, `prepack`, publish hooks, and arbitrary `pre<name>`/`post<name>` pairs.
- Lockfiles, workspaces, aliases, overrides, local/file/git/URL dependencies, bundled dependencies, and package-manager selection.
- `.npmrc` registry/scope overrides, proxies, certificate/auth settings, script policy, and environment expansion.
- Yarn project runtime selection, plugins, `yarn.config.cjs`, Plug'n'Play loaders, SDKs, patches, and network/checksum policy.
- pnpm `pnpmfile.cjs`, patches, catalogs, registries, build allow/deny policy, and shared-store behavior.
- `npx`, `npm exec`, `npm create`, or package-name commands that can fetch and execute code.
- Checked-in `node_modules`, `.yarn` artifacts, native `.node` modules, and command shims only when a common workflow reaches them.

npm documents install and command hooks in [npm scripts](https://docs.npmjs.com/cli/using-npm/scripts/). [Yarn configuration](https://yarnpkg.com/configuration/yarnrc) permits project-local runtime and plugin selection. [pnpm build approval](https://pnpm.io/cli/approve-builds) controls dependency install scripts, while [pnpm workspace settings](https://pnpm.io/settings) warn that registry environment expansion can leak secrets.

Inspect custom loaders, require hooks, test setup, transformers, reporters, coverage/bundler/linter plugins, Electron startup, and native addons only as early execution surfaces or when implicated by a lead. Do not review frontend/backend application security generally.

## Python

Inspect:

- `pyproject.toml` build-system requirements, backend, backend path, dynamic metadata, scripts, entry points, plugins, and dependency sources.
- `setup.py`, `setup.cfg`, requirements/constraints files, direct URLs, VCS/path/editable requirements, alternate indexes, trusted hosts, hashes, and installer configuration.
- Project-selected build backends; pip delegates builds and metadata generation to [executable backend hooks](https://pip.pypa.io/en/stable/reference/build-system/).
- `.pth`, `sitecustomize.py`, `usercustomize.py`, namespace/import hooks, console scripts, native extensions, and dynamic library loading.
- `pytest` `conftest.py`, auto-loaded plugins, collection imports, global fixtures, custom runners, tox/nox/Hatch/PDM/Poetry plugins, and notebook kernels.
- Checked-in virtual environments, activation scripts, interpreters, wheels, shared libraries, and caches only when referenced or suspicious; never source or execute them.

Search application and test files for attack-relevant combinations, then read only around a lead. Do not analyze API authorization, input validation, database logic, privacy behavior, or general Python vulnerabilities.

## Rust and Cargo

Inspect:

- `Cargo.toml`, `Cargo.lock`, workspace members, Git/path dependencies, patches, replacements, registries, and source replacement.
- `build.rs`; Cargo compiles and runs [build scripts](https://doc.rust-lang.org/cargo/reference/build-scripts.html) before building a package.
- `.cargo/config*` aliases, runners, compiler/doc wrappers, linkers, credential providers, environment injection, source replacement, and registry/network settings described by the [Cargo configuration reference](https://doc.rust-lang.org/cargo/reference/config.html).
- Procedural macros, native dependencies, `links`, code generation, linker arguments, custom test harnesses, and toolchain auto-download behavior.

Do not review ordinary unsafe-code correctness or Rust application logic without a hostile lead.

## Go

Inspect:

- `go.mod`, `go.sum`, `go.work`, replacements, local/VCS sources, module proxy/checksum settings, vendored modules, and toolchain directives under the [Go module model](https://go.dev/ref/mod).
- cgo, compiler/linker flags, native libraries, assembly, plugins, and external tool invocation.
- `go:generate` directives, generators, package `init`, `TestMain`, test bootstrap, and tool packages reached by common commands.

`go test` executes initialization and test code; `go generate` executes declared commands. Search broadly for high-signal effects but do not audit general package correctness.

## JVM, Gradle, and Maven

### Gradle

Inspect:

- Wrapper scripts/JAR/properties, distribution URL, and checksum. Gradle advises developers [not to run a wrapper from an untrusted project](https://docs.gradle.org/current/userguide/best_practices_security.html).
- `settings.gradle*`, `build.gradle*`, `gradle.properties`, convention plugins, `buildSrc`, included/composite builds, init/applied scripts, plugin repositories, and dependency verification.
- Configuration-time code, `Exec`/process use, listeners, custom plugins/tasks, annotation processors, code generation, test plugins, and publish/deploy logic when reachable.

### Maven

Inspect:

- `pom.xml` parents, profiles, repositories, plugin repositories, extensions, lifecycle-bound plugins, annotation processors, and publish/deploy plugins.
- Maven wrapper files, `.mvn/extensions.xml`, `.mvn/maven.config`, `.mvn/jvm.config`, and downloader configuration.

Maven's [build lifecycle](https://maven.apache.org/guides/introduction/introduction-to-the-lifecycle.html) runs configured plugins across validation, generation, compilation, testing, packaging, installation, and deployment.

Inspect JVM static initializers, agents, JNI, service loaders, test engines/listeners, and executable manifests only when they are early execution or part of a lead. Do not audit application serialization, authorization, or business logic generally.

## Ruby

Inspect:

- `Gemfile`, included gemfiles, gemspecs, lockfiles, alternate/Git/path sources, and plugins. Bundler states that a [Gemfile is evaluated as Ruby code](https://bundler.io/guides/gemfile.html).
- `Rakefile`, native-extension setup, install hooks, executables, framework initializers, generators, test helpers, RubyGems plugins, binstubs, and environment-manager configuration.

Do not invoke Bundler parsing, gemspec evaluation, or Rake task listing.

## PHP and Composer

Inspect:

- `composer.json`, lockfiles, scripts/events, plugins, installers, repositories, path/VCS/package sources, autoload files, executable bins, and platform overrides.
- `allow-plugins`, script policy, custom installers, autoload bootstrap, test listeners/extensions, framework commands, and migration hooks.

Composer [plugins load into the Composer process](https://getcomposer.org/doc/articles/plugins.md). Execution capability is not a hostile finding without deceptive behavior, compromise, or an attack-relevant effect.

## .NET, NuGet, and MSBuild

Inspect:

- Project/solution files, `Directory.Build.props`, `Directory.Build.targets`, imported targets, custom/inline tasks, analyzers, source generators, test adapters, and publish targets.
- `NuGet.Config`, package sources/mappings, local tools, workloads, packages with build/transitive targets, native assets, and PowerShell/batch build code.

MSBuild evaluation and imported targets are executable. Do not use restore, project listing, design-time build, or test discovery as audit probes.

## Native toolchains and build systems

Inspect:

- `configure`, Autoconf macros, Makefiles, install/uninstall rules, compiler/linker variables, included files, shell substitution, and recursive task invocation.
- CMake modules/presets/toolchains, `execute_process`, `try_run`, custom commands, compiler launchers, `FetchContent`, `ExternalProject`, install scripts, and test registration.
- Meson custom targets/generators/wraps; Conan/vcpkg recipes, hooks, overlays, registries; compiler wrappers, response/link scripts, constructors, and dynamic loader paths.

Configuration is execution-capable. A dry run may still expand shell functions or configuration; never invoke it during the audit. Do not inspect native application memory safety absent a hostile lead.

## Bazel and similar build systems

Inspect:

- `WORKSPACE*`, `MODULE.bazel`, `BUILD*`, `.bazelrc`, Starlark repository/module extensions, toolchains, tests, and remote execution/cache settings.
- Repository rules/downloads, patches, local repositories, credential helpers, environment inheritance, and command aliases.

Hermeticity does not make repository rules or remote services safe. Do not audit ordinary rule correctness beyond early execution and hostile-repository signals.

## Nix and environment managers

Inspect:

- `flake.nix`, lockfiles, shell expressions, overlays, fetchers, inputs, substituters, trusted keys, builders, impure access, and dev-shell hooks.
- `.envrc`, lorri/devenv, mise/asdf/rtx plugins/tasks/hooks, tool download URLs/checksums, environment files, and shims.

Do not enter a dev shell. Sandboxed builds may still consume credentials/configuration or run host-side hooks.

## Shell, Make, Just, and task runners

Inspect sourced/imported files, includes, evaluated variables, command substitutions, recursive invocation, environment exports, cleanup traps, background jobs, and platform branches.

Prioritize:

- Make `include`, `eval`, `shell`, generated prerequisites, implicit rules, and recursive makes.
- Just imports/modules, shell selection, dotenv loading, backticks, aliases, and dependencies.
- Taskfile includes, dynamic variables/sources, preconditions/status commands, watchers, and remote taskfiles.
- Pre-commit, Husky, lint-staged, release automation, and Git-hook installers.

Trace common root recipes far enough to identify unexpected effects; do not review every module task in a monorepo.

## Containers, devcontainers, infrastructure, and CI

Inspect early or privileged behavior:

- Docker remote `ADD`/downloads, package steps, build secrets, entrypoints, users, and files copied from target.
- Compose/devcontainer host paths, credentials, Docker socket, SSH agent, devices, privileged capabilities, host namespaces/network, and lifecycle commands.
- Kubernetes/Helm/Kustomize hooks/plugins, host mounts, service accounts, init/lifecycle jobs, and generators.
- Terraform/OpenTofu provisioners, `local-exec`/`remote-exec`, external data, providers/modules/backends, hooks, credential handling, and auto-apply paths.
- Pulumi/CDK/Ansible plugins/callbacks, deployment/release scripts, package publishers, signing, and artifact upload.
- CI triggers, tokens/secrets/OIDC, runners, checkouts, reusable workflows/actions, caches/artifacts, and untrusted metadata interpolation.

Privileged [`pull_request_target` execution](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target) can form a repository attack when untrusted content reaches a privileged job. GitHub's [2026 checkout hardening](https://github.blog/changelog/2026-06-18-safer-pull_request_target-defaults-for-github-actions-checkout/) blocks common fork-head patterns for supported checkout versions; verify actual reachability before escalating. Mutable action tags or broad permissions alone are hardening concerns, not hostile-repository findings.

Do not review infrastructure design, cloud least privilege, or CI quality generally. Trace only automatic execution and evidence-backed attack paths.
