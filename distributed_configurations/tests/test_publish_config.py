"""Self-contained checks for publish_config.py.

No PTR site and no network required - the site-side flow is exercised against a
stub connection.  Run with::

    python tests/test_publish_config.py
"""

import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import publish_config as p  # noqa: E402


PASSED = []
FAILED = []


def check(label, got, want):
    if got == want:
        PASSED.append(label)
        print("  PASS  %s" % label)
    else:
        FAILED.append(label)
        print("  FAIL  %s\n          got:  %r\n          want: %r" % (label, got, want))


def check_raises(label, fn, fragment):
    try:
        fn()
    except p.PublishError as error:
        check(label, fragment in str(error), True)
    else:
        check(label, "no error raised", "PublishError containing %r" % fragment)


# ---------------------------------------------------------------- descriptors

def test_descriptor_uris():
    print("\n[descriptor uris]")
    uri = "sgtk:descriptor:git?path=git@github.com:studio/tk-config.git&version=v1.0.0"
    check("parse", p.uri_to_dict(uri),
          {"type": "git", "path": "git@github.com:studio/tk-config.git", "version": "v1.0.0"})
    check("round-trip", p.dict_to_uri(p.uri_to_dict(uri)), uri)
    check("bump git tag", p.build_git_descriptor(uri, None, "v2.0.0", None, None),
          "sgtk:descriptor:git?path=git@github.com:studio/tk-config.git&version=v2.0.0")

    branch = ("sgtk:descriptor:git_branch?branch=main"
              "&path=https://github.com/studio/tk-config.git&version=abc123")
    check("bump git_branch sha", p.build_git_descriptor(branch, None, "def456", None, None),
          "sgtk:descriptor:git_branch?branch=main"
          "&path=https://github.com/studio/tk-config.git&version=def456")

    check("unc path stays readable",
          p.dict_to_uri({"type": "git", "path": r"\\server\share\tk-config.git", "version": "v1"}),
          r"sgtk:descriptor:git?path=\\server\share\tk-config.git&version=v1")
    check("brand new descriptor",
          p.build_git_descriptor(None, "https://x/y.git", "v1.0.0", "git", None),
          "sgtk:descriptor:git?path=https://x/y.git&version=v1.0.0")

    check_raises("git_branch demands --branch",
                 lambda: p.build_git_descriptor(None, "https://x/y.git", "sha", "git_branch", None),
                 "--branch")
    check_raises("no existing descriptor demands --git-url",
                 lambda: p.build_git_descriptor(None, None, "v1.0.0", None, None),
                 "no --git-url")
    check_raises("refuses to clobber an app_store descriptor",
                 lambda: p.build_git_descriptor(
                     "sgtk:descriptor:app_store?name=tk-config-default2&version=v1",
                     None, "v2", None, None),
                 "not a git descriptor")


# ----------------------------------------------------------------- precedence

def test_precedence():
    print("\n[field precedence - tk-core bootstrap/resolver.py]")
    check("path field blocks an upload",
          p.inspect_precedence({"id": 1, "windows_path": r"C:\cfg"}, "upload")[0], "centralized")
    check("descriptor field blocks an upload",
          p.inspect_precedence(
              {"id": 1, "descriptor": "sgtk:descriptor:git?path=x&version=v1",
               "plugin_ids": "basic.*"}, "upload")[0],
          "descriptor_wins")
    check("descriptor mode is not blocked by its own descriptor",
          p.inspect_precedence(
              {"id": 1, "descriptor": "sgtk:descriptor:git?path=x&version=v1",
               "plugin_ids": "basic.*"}, "descriptor")[0],
          None)

    code, _msg, notes = p.inspect_precedence({"id": 1, "plugin_ids": None}, "upload")
    check("empty plugin_ids warns but does not block", (code, len(notes)), (None, 1))

    code, _msg, notes = p.inspect_precedence(
        {"id": 1, "plugin_ids": "basic.*", "uploaded_config": {"id": 9}}, "descriptor")
    check("stale attachment noted in descriptor mode", (code, len(notes)), (None, 1))

    check("healthy distributed config passes",
          p.inspect_precedence({"id": 1, "plugin_ids": "basic.*"}, "upload"), (None, None, []))


# ------------------------------------------------------------------ packaging

def build_fixture(root, nested_in=None):
    """Write a minimal but realistic config tree; return the config root."""
    base = os.path.join(root, nested_in) if nested_in else root
    files = {
        "info.yml": 'display_name: "Studio Config"\n',
        "core/roots.yml": "primary:\n  default: true\n  linux_path: null\n"
                          "  mac_path: null\n  windows_path: null\n",
        "core/core_api.yml": "location:\n  type: app_store\n  name: tk-core\n  version: v0.23.8\n",
        "env/project.yml": "engines:\n  tk-maya:\n    location: {type: app_store, "
                           "name: tk-maya, version: v0.10.1}\n",
        "hooks/example.py": "pass\n",
        ".github/workflows/ci.yml": "name: ci\n",
        "__pycache__/junk.pyc": "x\n",
    }
    for rel, body in files.items():
        full = os.path.join(base, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(body)
    return base


def test_excludes():
    print("\n[exclude matching]")
    pats = p.DEFAULT_EXCLUDES
    check("prunes .github at root", p.is_excluded(".github/workflows/ci.yml", pats), True)
    check("prunes nested __pycache__", p.is_excluded("core/hooks/__pycache__/a.pyc", pats), True)
    check("prunes tk-metadata", p.is_excluded("tk-metadata/foo.yml", pats), True)
    check("keeps real config files", p.is_excluded("core/roots.yml", pats), False)
    check("keeps hooks", p.is_excluded("hooks/tk-multi-publish2/collector.py", pats), False)


def test_zip_layout():
    print("\n[zip layout]")
    with tempfile.TemporaryDirectory() as tmp:
        src = build_fixture(os.path.join(tmp, "repo"))
        staged = os.path.join(tmp, "staged")
        os.makedirs(staged)
        p.copy_working_tree(src, staged, p.DEFAULT_EXCLUDES)
        zip_path = os.path.join(tmp, "out.zip")
        p.make_zip(staged, zip_path)

        names = sorted(zipfile.ZipFile(zip_path).namelist())
        check("config sits at the zip root", names,
              ["core/core_api.yml", "core/roots.yml", "env/project.yml",
               "hooks/example.py", "info.yml"])

        # tk-core unwraps a lone top-level folder (util/zip.py auto_detect_bundle),
        # which would silently destroy the layout - make sure we never emit that.
        roots = set(n.split("/")[0] for n in names if "/" in n)
        check("more than one top-level dir, so auto_detect_bundle won't unwrap",
              len(roots) > 1, True)


def test_config_root_detection():
    print("\n[config root detection]")
    with tempfile.TemporaryDirectory() as tmp:
        flat = os.path.join(tmp, "flat")
        build_fixture(flat)
        check("repo root is the config root", p.find_config_root(flat), flat)

        nested = os.path.join(tmp, "nested")
        build_fixture(nested, nested_in="config")
        check("config/ subfolder", p.find_config_root(nested),
              os.path.join(nested, "config"))

        wrapper = os.path.join(tmp, "zipball")
        build_fixture(wrapper, nested_in="tk-config-studio-1.0.0")
        check("zipball wrapper unwrapped when allowed",
              p.find_config_root(wrapper, allow_wrapper=True),
              os.path.join(wrapper, "tk-config-studio-1.0.0"))
        check_raises("zipball wrapper NOT unwrapped for a git checkout",
                     lambda: p.find_config_root(wrapper),
                     "Could not find a Toolkit config root")


def test_validation():
    print("\n[validation]")
    with tempfile.TemporaryDirectory() as tmp:
        good = build_fixture(os.path.join(tmp, "good"))
        report = p.Report()
        p.validate_config(good, report)
        check("clean config has no errors", report.errors, [])
        check("clean config has no warnings", report.warnings, [])

        bad = build_fixture(os.path.join(tmp, "bad"))
        with open(os.path.join(bad, "core", "roots.yml"), "w") as handle:
            handle.write("primary:\n  default: true\n  linux_path: null\n"
                         "  mac_path: null\n  windows_path: C:\\studio\\projects\n")
        with open(os.path.join(bad, "core", "pipeline_configuration.yml"), "w") as handle:
            handle.write("pc_id: 925\npc_name: Primary\nproject_id: 881\n")
        with open(os.path.join(bad, "core", "shotgun.yml"), "w") as handle:
            handle.write("host: https://studio.shotgrid.autodesk.com\n"
                         "api_script: toolkit\napi_key: sekrit\n")
        with open(os.path.join(bad, "env", "dev.yml"), "w") as handle:
            handle.write("apps:\n  tk-multi-publish2:\n"
                         "    location: {type: dev, path: /mnt/dev/tk-multi-publish2}\n")
        report = p.Report()
        p.validate_config(bad, report)
        joined = " | ".join(report.warnings)
        check("baked storage path warned", "hard-coded storage paths" in joined, True)
        check("null paths NOT reported as baked", "linux_path" in joined, False)
        check("installed pipeline_configuration.yml warned",
              "pipeline config id" in joined, True)
        check("dev descriptor warned", "dev/path descriptor" in joined, True)
        check("committed api_key is an error, not a warning",
              any("api_key" in e for e in report.errors), True)

        missing = os.path.join(tmp, "missing")
        os.makedirs(os.path.join(missing, "core"))
        report = p.Report()
        p.validate_config(missing, report)
        check("missing env/ is an error", any("env/" in e for e in report.errors), True)
        check("lone top-level dir is an error",
              any("single top-level folder" in e for e in report.errors), True)


# --------------------------------------------------------------- publish flow

class StubShotgun:
    """Just enough of the shotgun_api3 surface for publish()."""

    def __init__(self, entities):
        self.entities = {e["id"]: dict(e) for e in entities}
        self.uploads = []
        self.updates = []
        self._next_attachment = 5000

    def find(self, _entity_type, filters, fields, order=None):
        ids = None
        names = None
        for field, op, value in filters:
            if field == "id" and op == "in":
                ids = value
            if field == "code" and op == "in":
                names = value
        out = []
        for entity in self.entities.values():
            if ids is not None and entity["id"] not in ids:
                continue
            if names is not None and entity.get("code") not in names:
                continue
            out.append({f: entity.get(f) for f in fields})
        return sorted(out, key=lambda e: e["id"])

    def schema_field_read(self, _entity_type):
        return {name: {} for name in
                ["id", "code", "project", "plugin_ids", "descriptor",
                 "uploaded_config", "windows_path", "linux_path", "mac_path"]}

    def update(self, _entity_type, entity_id, data):
        self.updates.append((entity_id, dict(data)))
        self.entities[entity_id].update(data)
        return self.entities[entity_id]

    def upload(self, _entity_type, entity_id, path, field_name=None, display_name=None):
        self._next_attachment += 1
        self.uploads.append((entity_id, os.path.basename(path), field_name, display_name))
        self.entities[entity_id][field_name] = {
            "id": self._next_attachment, "link_type": "upload", "name": display_name}
        return self._next_attachment


class Args:
    """Mirror of the argparse namespace fields publish() reads."""

    def __init__(self, **kwargs):
        defaults = dict(
            mode="upload", pipeline_config_id=[], config_name=[], project_id=None,
            site_wide=False, all_projects=False, create_if_missing=False,
            clear_descriptor=False, clear_uploaded_config=False, set_plugin_ids=False,
            plugin_ids=None, force=False, dry_run=False, version=None, git_url=None,
            descriptor_type=None, branch=None,
        )
        defaults.update(kwargs)
        self.__dict__.update(defaults)


def test_publish_flow():
    print("\n[publish flow against a stub site]")
    zip_path = os.path.join(tempfile.mkdtemp(), "primary-v2.0.0.zip")
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("info.yml", "x")

    # --- upload blocked by an existing git descriptor -------------------
    sg = StubShotgun([{
        "id": 925, "code": "Primary", "project": {"type": "Project", "id": 881, "name": "demo"},
        "plugin_ids": "basic.*",
        "descriptor": "sgtk:descriptor:git?path=https://github.com/studio/tk-config.git&version=v1.0.0",
    }])
    args = Args(project_id=881, config_name=["Primary"], version="v2.0.0")
    check_raises("upload refuses while a descriptor is set",
                 lambda: p.publish(args, sg, zip_path, {}),
                 "takes precedence")
    check("nothing was uploaded", sg.uploads, [])

    # --- same thing, but we opted into switching modes ------------------
    args = Args(project_id=881, config_name=["Primary"], version="v2.0.0",
                clear_descriptor=True)
    p.publish(args, sg, zip_path, {})
    check("descriptor blanked", sg.updates, [(925, {"descriptor": None})])
    check("zip uploaded to uploaded_config",
          sg.uploads, [(925, "primary-v2.0.0.zip", "uploaded_config", "primary-v2.0.0.zip")])

    # --- centralized config is refused ----------------------------------
    sg2 = StubShotgun([{"id": 7, "code": "Primary", "project": None,
                        "windows_path": r"C:\studio\config"}])
    args = Args(pipeline_config_id=[7], version="v2.0.0", clear_descriptor=True)
    check_raises("centralized config refused even with --clear-descriptor",
                 lambda: p.publish(args, sg2, zip_path, {}),
                 "centralized config")

    # --- descriptor mode bumps the tag in place -------------------------
    sg3 = StubShotgun([{
        "id": 925, "code": "Primary", "project": {"type": "Project", "id": 881, "name": "demo"},
        "plugin_ids": "basic.*",
        "descriptor": "sgtk:descriptor:git?path=https://github.com/studio/tk-config.git&version=v1.0.0",
    }])
    args = Args(mode="descriptor", project_id=881, config_name=["Primary"], version="v2.0.0")
    p.publish(args, sg3, None, {})
    check("descriptor bumped to the new tag", sg3.updates,
          [(925, {"descriptor": "sgtk:descriptor:git?"
                                "path=https://github.com/studio/tk-config.git&version=v2.0.0"})])

    # --- dry run touches nothing ----------------------------------------
    sg4 = StubShotgun([{"id": 925, "code": "Primary", "project": None, "plugin_ids": "basic.*"}])
    args = Args(pipeline_config_id=[925], version="v2.0.0", dry_run=True)
    p.publish(args, sg4, zip_path, {})
    check("dry run performed no uploads", sg4.uploads, [])
    check("dry run performed no updates", sg4.updates, [])

    # --- plugin_ids backfill --------------------------------------------
    sg5 = StubShotgun([{"id": 42, "code": "Primary", "project": None, "plugin_ids": None}])
    args = Args(pipeline_config_id=[42], version="v2.0.0", set_plugin_ids=True)
    p.publish(args, sg5, zip_path, {})
    check("plugin_ids backfilled", sg5.updates, [(42, {"plugin_ids": "basic.*"})])


def main():
    test_descriptor_uris()
    test_precedence()
    test_excludes()
    test_zip_layout()
    test_config_root_detection()
    test_validation()
    test_publish_flow()
    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("failures: %s" % ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
