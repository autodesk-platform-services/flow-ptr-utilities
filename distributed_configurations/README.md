# Automated config releases for Flow Production Tracking

Automates the distributed-config workflow from
[Phil's community post](https://community.shotgridsoftware.com/t/distributed-config-management-workflow/232):

| Phil's manual step | Here |
| --- | --- |
| Develop locally | unchanged |
| Track changes with Git | unchanged |
| Create a GitHub Release | unchanged — this is the trigger |
| Download the ZIP from GitHub | done for you, and **re-rooted + linted** first |
| Upload into the Uploaded Config field | done for you, with the precedence trap handled |

One script, two ways to run it:

* **Automatically**, from a GitHub Actions `release` trigger.
* **Manually**, from a clone on any machine that can reach the PTR site — for
  studios with no GitHub access. Same code path, same artifact.

## Layout

```
fptr_config_release/
├── publish_config.py            the whole tool, no imports beyond shotgun_api3
├── requirements.txt
├── gitattributes.example        export-ignore rules for your config repo
├── tests/test_publish_config.py 44 checks, no site or network needed
└── workflows/
    ├── publish-config.yml       copy to .github/workflows/
    └── validate-config.yml      optional PR gate, no secrets needed
```

## Install into your config repo

```bash
mkdir -p tools/fptr_config_release .github/workflows
cp publish_config.py requirements.txt tools/fptr_config_release/
cp -r tests tools/fptr_config_release/
cp workflows/publish-config.yml .github/workflows/
cp workflows/validate-config.yml .github/workflows/   # optional
cat gitattributes.example >> .gitattributes           # review before committing
```

Then set these in **Settings → Secrets and variables → Actions**:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `FPTR_SCRIPT_NAME` | API Script user name |
| Secret | `FPTR_API_KEY` | its application key |
| Variable | `FPTR_SITE` | `https://yourstudio.shotgrid.autodesk.com` |
| Variable | `FPTR_PROJECT_ID` | project id, or leave unset for a site-wide config |
| Variable | `FPTR_CONFIG_NAME` | defaults to `Primary` |
| Variable | `FPTR_STAGING_CONFIG_NAME` | pre-release target, defaults to `Staging` |

The script user needs read/write on `PipelineConfiguration` and nothing else.

Publish a release and the workflow builds the zip, validates it, uploads it,
attaches the artifact back to the release, and writes a summary. Pre-releases go
to the staging config so artists never see an untested one.

## The trap this exists to avoid

`tk-core`'s bootstrap resolver picks a config by **field precedence**, not by
"most recently changed"
([`bootstrap/resolver.py:407-414`](https://github.com/shotgunsoftware/tk-core/blob/master/python/tank/bootstrap/resolver.py)):

```
1. windows/linux/mac path   (centralized config)
2. descriptor
3. sg_descriptor
4. uploaded_config
5. sg_uploaded_config
```

**`descriptor` beats `uploaded_config`.** So if you are on a git descriptor
today and you upload a zip, *nothing happens* — Toolkit keeps resolving the git
descriptor, your artists keep getting the old config, and there is no error
message anywhere. This is the single most common way this migration goes wrong.

`publish_config.py` refuses to publish into that state and tells you exactly
which field is shadowing which. `--clear-descriptor` blanks the descriptor in
the same update that performs the upload. Same story for a centralized config
whose `windows_path` is set — that one is refused outright, because clearing a
path field is not a decision a release script should make for you.

Since you are on a git descriptor, you have two supported directions:

```bash
# A. keep git as the source of truth, just move it to the new tag
#    (artists need git + network access to the repo)
python publish_config.py --mode descriptor --version v2.1.0 --project-id 881

# B. switch to uploaded zips
#    (artists need nothing but the PTR site)
python publish_config.py --version v2.1.0 --project-id 881 --clear-descriptor
```

Mode A is a one-field update and is the smallest possible change to what you
run today. Mode B is what Phil's post describes and what the workflow does by
default.

## Why not just upload GitHub's release zip?

GitHub's auto-generated source zip nests everything under `<repo>-<tag>/`.
Toolkit's unzip does try to unwrap a single top-level folder
([`util/zip.py:43-72`](https://github.com/shotgunsoftware/tk-core/blob/master/python/tank/util/zip.py)),
so it often works — but:

* it breaks if your config lives in a `config/` subfolder, because unwrapping
  then leaves `config/` and `.github/` at the root rather than `core/` and `env/`;
* it ships `.github/`, `tests/`, `__pycache__` and anything else in the repo to
  every artist;
* it can't be linted or version-stamped on the way through.

This script builds the zip with `git archive` (so `.gitattributes export-ignore`
applies), prunes the junk, and always emits `core/`, `env/`, `hooks/` at the zip
root. If you *do* hand it a GitHub zipball via `--zip`, it re-roots it for you.

## Validation

Every build is linted before it can be published. Errors block; warnings are
advisory unless you pass `--strict`.

| Check | Level | Why |
| --- | --- | --- |
| `core/` or `env/` missing | error | not a config |
| exactly one top-level folder | error | Toolkit would unwrap it and drop root files |
| `api_script`/`api_key` in `core/shotgun.yml` | error | never ship credentials to artists |
| `type: dev` / `type: path` in `env/` | warning | resolves to a local disk, breaks elsewhere |
| hard-coded paths in `core/roots.yml` | warning | distributed configs resolve roots from Local Storages |
| real `pc_id` in `core/pipeline_configuration.yml` | warning | you committed an *installed* config |
| real paths in `core/install_location.yml` | warning | same |
| zip over 100 MB | warning | every artist pays this on first bootstrap |

The last two are cleaned up automatically by `--strip-installed-state`, which
the CI workflow passes.

> Heads up: the config at `fptr_configs/config` in this workspace trips three of
> these — it's an installed config (real `pc_id` 925, a `windows_path` baked into
> `roots.yml`, a local `install_location.yml`). Publish from the *source* repo,
> not from an installed config folder.

## Offline / no-internet studios

Nothing in the manual path touches GitHub. From a clone:

```bash
git fetch --tags
python publish_config.py --ref v2.1.0 --version v2.1.0 \
  --project-id 881 --config-name Primary --clear-descriptor
```

If even the build machine and the PTR-connected machine are different, split it
in two:

```bash
# on the machine with the repo
python publish_config.py --ref v2.1.0 --version v2.1.0 \
  --zip-only --output /mnt/transfer/primary-v2.1.0.zip

# on the machine that can reach the site
python publish_config.py --zip /mnt/transfer/primary-v2.1.0.zip \
  --version v2.1.0 --pipeline-config-id 925 --clear-descriptor
```

`shotgun_api3` install without PyPI access: `pip install --no-index
--find-links <wheelhouse> shotgun-api3`, or point `--tk-core` at a tk-core
install that vendors it. PyYAML is optional — without it the script falls back
to a line scan and keeps going.

## Everyday flags

```bash
--dry-run              connect, resolve targets, report, change nothing
--zip-only             build + validate, never touch the network
--strict               warnings become errors
--verbose              show pruning decisions and full tracebacks
--set-plugin-ids       set plugin_ids to basic.* when empty
--create-if-missing    create the Pipeline Configuration (needs --project-id)
--force                publish anyway when precedence says it will be ignored
```

Targeting, in order of precedence: `--pipeline-config-id` (repeatable) >
`--project-id` + `--config-name` > `--site-wide` > `--all-projects`.

## Rolling back

The attachment id changes on every upload, which is exactly how clients detect
a new config, so a rollback is just a re-publish of the older tag:

```bash
python publish_config.py --ref v2.0.0 --version v2.0.0 --project-id 881
```

Or re-run the release workflow manually with `tag: v2.0.0`. Because the workflow
also attaches each built zip to its release, you can always `--zip` the exact
bytes that were published previously rather than rebuilding them.

## Version stamping

Each zip gets a `config_release.yml` at its root recording the tag, commit sha,
repo, build time and CI run:

```yaml
version: "v2.1.0"
commit: "0af81f8b6225eed868292c3f3b0d9216e47315c3"
describe: "v2.1.0"
repository: "https://github.com/studio/tk-config-studio.git"
built_at: "2026-08-24T04:57:00Z"
built_by: "felix.onsare"
workflow_run: "1234567890"
```

When an artist reports something odd, that file in their bundle cache tells you
precisely which commit they are running. Disable with `--no-stamp`.

## Tests

```bash
python tests/test_publish_config.py
```

44 checks covering descriptor URI round-tripping, the field-precedence rules,
exclude matching, zip layout, config-root detection, every validation rule, and
the publish flow against a stub site. No PTR site, no network, no credentials.
