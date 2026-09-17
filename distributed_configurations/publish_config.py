#!/usr/bin/env python
"""
Publish a Toolkit pipeline configuration to Flow Production Tracking.

Automates the manual workflow described in
https://community.shotgridsoftware.com/t/distributed-config-management-workflow/232

    develop locally -> git -> GitHub Release -> download zip -> upload to the
    Pipeline Configuration's "Uploaded Config" field

The same entry point covers both operating modes:

  * CI       - run from a GitHub Actions ``release`` trigger (see
               workflows/publish-config.yml).
  * Manual   - run by hand from a clone on any machine that can reach the PTR
               site.  No GitHub access required; the zip is built straight from
               a git tag (or from the working tree).

Two publish modes are supported, matching the two distributed-config styles:

  --mode upload      build a zip and upload it to PipelineConfiguration.uploaded_config
  --mode descriptor  leave the config in git and just re-point the
                     PipelineConfiguration.descriptor field at the new tag

Field precedence at bootstrap time (tk-core ``bootstrap/resolver.py``) is:

    1. windows/linux/mac path   (centralized config)
    2. descriptor
    3. sg_descriptor
    4. uploaded_config
    5. sg_uploaded_config

which means an uploaded zip is *silently ignored* while a descriptor URI is
still set on the same Pipeline Configuration.  This script refuses to leave you
in that state - see --clear-descriptor / --clear-uploaded-config.

Requires Python 3.7+ and, for anything that talks to the site, ``shotgun_api3``.

Exit codes:
    0  success (or nothing to do)
    1  runtime / connection / upload error
    2  configuration validation failed, or bad usage
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlparse

try:  # optional: makes a few validation checks more precise
    import yaml
except ImportError:  # pragma: no cover - PyYAML is not required
    yaml = None


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PC_ENTITY = "PipelineConfiguration"
UPLOAD_FIELD = "uploaded_config"
DESCRIPTOR_FIELD = "descriptor"
DEFAULT_PLUGIN_IDS = "basic.*"
DEFAULT_CONFIG_NAME = "Primary"
PATH_FIELDS = ("windows_path", "linux_path", "mac_path")

# Files that must never end up inside an uploaded config.  ``tk-metadata`` and
# the pycache entries mirror what tk-core itself strips before uploading in
# ``commands/setup_project_params.py``.
DEFAULT_EXCLUDES = [
    ".git",
    ".github",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "Thumbs.db",
    "tk-metadata",
    ".cached_metadata.pickle",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "*.zip",
]

# Files tk-core regenerates when it caches a config locally.  Harmless but they
# carry another site's state, so we offer to drop them.
INSTALLED_STATE_FILES = [
    "core/pipeline_configuration.yml",
    "core/install_location.yml",
]

DEFAULT_STAMP_FILE = "config_release.yml"

# soft limit before we start warning about attachment size
LARGE_ZIP_BYTES = 100 * 1024 * 1024


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

_VERBOSE = False


def log(msg=""):
    sys.stdout.write("%s\n" % msg)
    sys.stdout.flush()


def debug(msg):
    if _VERBOSE:
        log("  . %s" % msg)


def step(msg):
    log("\n==> %s" % msg)


class PublishError(Exception):
    """Fatal, user-facing error."""


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

def git(args, cwd, check=True):
    proc = subprocess.run(
        ["git"] + list(args),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if check and proc.returncode != 0:
        raise PublishError(
            "git %s failed in %s:\n%s" % (" ".join(args), cwd, proc.stderr.strip())
        )
    return proc.stdout.strip(), proc.returncode


def git_repo_root(path):
    try:
        out, rc = git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    except OSError:
        return None
    return out or None if rc == 0 else None


def git_describe(repo, ref):
    """Return (sha, described_name) for ``ref``, tolerating repos without tags."""
    sha, _ = git(["rev-parse", ref], cwd=repo)
    described, rc = git(["describe", "--tags", "--always", ref], cwd=repo, check=False)
    return sha, (described if rc == 0 else sha[:8])


def git_remote_url(repo):
    out, rc = git(["config", "--get", "remote.origin.url"], cwd=repo, check=False)
    return out if rc == 0 and out else None


def git_is_dirty(repo):
    out, rc = git(["status", "--porcelain"], cwd=repo, check=False)
    return rc == 0 and bool(out)


# --------------------------------------------------------------------------
# staging the config payload
# --------------------------------------------------------------------------

def is_excluded(rel_path, patterns):
    """Match ``rel_path`` (posix, relative) against exclude patterns.

    A pattern matches if it matches the whole relative path or any single path
    component, so ``__pycache__`` prunes the directory at any depth.
    """
    parts = rel_path.split("/")
    for index, part in enumerate(parts):
        subpath = "/".join(parts[: index + 1])
        for pattern in patterns:
            if fnmatch.fnmatch(part, pattern) or fnmatch.fnmatch(subpath, pattern):
                return True
    return False


def _on_rm_error(func, path, _exc_info):
    """Windows: clear the read-only bit git leaves on some objects."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path, ignore_errors=False):
    """shutil.rmtree that survives read-only files on Windows."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, ignore_errors=ignore_errors,
                      onexc=lambda f, p, e: _on_rm_error(f, p, e))
    else:
        shutil.rmtree(path, ignore_errors=ignore_errors, onerror=_on_rm_error)


def extract_zip(zip_path, dest):
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(dest)


def export_git_ref(repo, ref, dest):
    """Export ``ref`` from ``repo`` into ``dest`` honouring .gitattributes."""
    with tempfile.TemporaryDirectory(prefix="fptr-archive-") as tmp:
        archive = os.path.join(tmp, "ref.zip")
        git(["archive", "--format=zip", "--output=%s" % archive, ref], cwd=repo)
        extract_zip(archive, dest)


def copy_working_tree(src, dest, patterns):
    for root, dirs, files in os.walk(src):
        rel_root = os.path.relpath(root, src).replace(os.sep, "/")
        rel_root = "" if rel_root == "." else rel_root
        dirs[:] = [
            d
            for d in dirs
            if not is_excluded(posixpath.join(rel_root, d) if rel_root else d, patterns)
        ]
        for name in files:
            rel = posixpath.join(rel_root, name) if rel_root else name
            if is_excluded(rel, patterns):
                continue
            target = os.path.join(dest, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(root, name), target)


def prune(root, patterns):
    """Remove excluded entries from an already-staged tree."""
    removed = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for name in dirnames:
            rel = posixpath.join(rel_dir, name) if rel_dir else name
            if is_excluded(rel, patterns):
                rmtree(os.path.join(dirpath, name))
                removed.append(rel + "/")
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            rel = posixpath.join(rel_dir, name) if rel_dir else name
            if is_excluded(rel, patterns):
                os.remove(os.path.join(dirpath, name))
                removed.append(rel)
    for item in removed:
        debug("pruned %s" % item)
    return removed


def looks_like_config_root(path):
    return os.path.isdir(os.path.join(path, "core")) and os.path.isdir(
        os.path.join(path, "env")
    )


def find_config_root(base, explicit_subdir=None, allow_wrapper=False, _depth=0):
    """Locate the folder that should sit at the *root* of the uploaded zip.

    Handles the shapes we see in the wild:
      * repo root is the config root (tk-config-default2 style)
      * config lives in a ``config/`` subfolder
      * a GitHub source zipball, where everything is nested under
        ``<repo>-<tag>/`` - only unwrapped when ``allow_wrapper`` is set, since
        for a git checkout descending into an arbitrary subfolder would be a
        guess rather than a fact.
    """
    if explicit_subdir:
        candidate = os.path.join(base, explicit_subdir.replace("/", os.sep))
        if not os.path.isdir(candidate):
            raise PublishError("--config-subdir '%s' not found under %s" % (explicit_subdir, base))
        return candidate

    if looks_like_config_root(base):
        return base

    nested = os.path.join(base, "config")
    if looks_like_config_root(nested):
        debug("config root found in config/ subfolder")
        return nested

    if allow_wrapper and _depth < 2:
        children = [
            os.path.join(base, name)
            for name in sorted(os.listdir(base))
            if os.path.isdir(os.path.join(base, name))
        ]
        if len(children) == 1:
            debug("unwrapping single top-level folder '%s'" % os.path.basename(children[0]))
            return find_config_root(children[0], None, True, _depth + 1)

    raise PublishError(
        "Could not find a Toolkit config root under %s - expected a folder "
        "containing both 'core/' and 'env/'. Pass --config-subdir to point at it." % base
    )


def write_stamp(config_root, filename, data):
    lines = [
        "# Generated by publish_config.py - do not edit by hand.",
        "# Identifies exactly which commit produced the config you are running.",
    ]
    for key in sorted(data):
        value = data[key]
        lines.append("%s: %s" % (key, "null" if value is None else '"%s"' % value))
    lines.append("")
    with open(os.path.join(config_root, filename), "w") as handle:
        handle.write("\n".join(lines))
    debug("wrote %s" % filename)


def make_zip(config_root, zip_path):
    """Zip ``config_root`` so that core/, env/, hooks/ sit at the zip root."""
    os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
    file_count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirpath, dirnames, filenames in os.walk(config_root):
            dirnames.sort()
            rel_dir = os.path.relpath(dirpath, config_root).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            if rel_dir and not dirnames and not filenames:
                archive.writestr(rel_dir + "/", "")  # preserve empty folders
            for name in sorted(filenames):
                rel = posixpath.join(rel_dir, name) if rel_dir else name
                archive.write(os.path.join(dirpath, name), rel)
                file_count += 1
    return file_count


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)

    def emit(self, strict):
        for message in self.warnings:
            log("  [warn]  %s" % message)
        for message in self.errors:
            log("  [ERROR] %s" % message)
        if not self.errors and not self.warnings:
            log("  all checks passed")
        failed = bool(self.errors) or (strict and bool(self.warnings))
        return failed


def _load_yaml(path):
    if yaml is None or not os.path.isfile(path):
        return None
    try:
        with open(path) as handle:
            return yaml.safe_load(handle)
    except Exception as error:  # noqa: BLE001 - validation must never crash
        debug("could not parse %s: %s" % (path, error))
        return None


EMPTY_YAML_SCALARS = {"", "null", "~", "''", '""'}


def _scalar_after_key(line):
    """``  windows_path: C:\\foo`` -> ``C:\\foo``. Empty-ish values -> ``None``."""
    _, _, value = line.partition(":")
    value = value.strip()
    return None if value.lower() in EMPTY_YAML_SCALARS else value


def _grep(path, pattern):
    hits = []
    try:
        with open(path, "r", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                if re.search(pattern, line):
                    hits.append((number, line.rstrip()))
    except OSError:
        pass
    return hits


def validate_config(config_root, report):
    """Structural + distributed-config sanity checks."""
    for required in ("core", "env"):
        if not os.path.isdir(os.path.join(config_root, required)):
            report.error("missing required folder '%s/' at the config root" % required)

    if not os.path.isfile(os.path.join(config_root, "info.yml")):
        report.warn("no info.yml at the config root (Toolkit reads requires_core_version from it)")

    top_level_dirs = [
        name
        for name in os.listdir(config_root)
        if os.path.isdir(os.path.join(config_root, name))
    ]
    if len(top_level_dirs) == 1:
        report.error(
            "the zip would contain a single top-level folder ('%s/'); Toolkit's "
            "auto-detect would unwrap it and drop root-level files" % top_level_dirs[0]
        )

    # --- roots.yml -------------------------------------------------------
    roots_path = os.path.join(config_root, "core", "roots.yml")
    if not os.path.isfile(roots_path):
        report.warn("core/roots.yml is missing - Toolkit will assume the config has no storage roots")
    else:
        baked = []
        data = _load_yaml(roots_path)
        if isinstance(data, dict):
            for root_name, root_data in data.items():
                if not isinstance(root_data, dict):
                    continue
                for field in PATH_FIELDS:
                    if root_data.get(field):
                        baked.append("%s.%s = %s" % (root_name, field, root_data[field]))
        else:  # no PyYAML - fall back to a line scan of the generated format
            for _, line in _grep(roots_path, r"^\s+(windows|linux|mac)_path:"):
                if _scalar_after_key(line) is not None:
                    baked.append(line.strip())
        if baked:
            report.warn(
                "core/roots.yml has hard-coded storage paths (%s). Distributed configs "
                "should leave the *_path entries null and resolve them from the PTR "
                "Local Storages via shotgun_storage_id." % "; ".join(baked)
            )

    # --- leftovers from an *installed* config ----------------------------
    pc_yml = os.path.join(config_root, "core", "pipeline_configuration.yml")
    if os.path.isfile(pc_yml):
        data = _load_yaml(pc_yml) or {}
        pc_id = data.get("pc_id") if isinstance(data, dict) else None
        if pc_id is None and yaml is None:
            hits = _grep(pc_yml, r"^\s*pc_id:\s*\d+")
            pc_id = hits[0][1] if hits else None
        if pc_id:
            report.warn(
                "core/pipeline_configuration.yml carries a real pipeline config id (%s). "
                "This file is regenerated when Toolkit caches the config, so it is safe "
                "to remove - use --strip-installed-state." % pc_id
            )

    install_yml = os.path.join(config_root, "core", "install_location.yml")
    if os.path.isfile(install_yml):
        data = _load_yaml(install_yml)
        values = [v for v in (data or {}).values() if v] if isinstance(data, dict) else []
        if not values and yaml is None:
            values = [
                line.strip()
                for _, line in _grep(install_yml, r"^\s*(Windows|Darwin|Linux):")
                if _scalar_after_key(line) is not None
            ]
        if values:
            report.warn(
                "core/install_location.yml points at a local install (%s). It is only "
                "meaningful for centralized configs - use --strip-installed-state."
                % ", ".join(str(v) for v in values)
            )

    # --- secrets ---------------------------------------------------------
    shotgun_yml = os.path.join(config_root, "core", "shotgun.yml")
    if os.path.isfile(shotgun_yml):
        if _grep(shotgun_yml, r"^\s*api_(script|key):\s*\S+"):
            report.error(
                "core/shotgun.yml contains api_script/api_key credentials. Never ship "
                "script keys inside a config - remove them and rotate the key."
            )

    # --- dev / path descriptors in the environments ----------------------
    dev_hits = []
    env_root = os.path.join(config_root, "env")
    for dirpath, _dirnames, filenames in os.walk(env_root):
        for name in filenames:
            if not name.endswith((".yml", ".yaml")):
                continue
            full = os.path.join(dirpath, name)
            for number, line in _grep(full, r"type:\s*['\"]?(dev|path)\b"):
                rel = os.path.relpath(full, config_root).replace(os.sep, "/")
                dev_hits.append("%s:%s %s" % (rel, number, line.strip()))
    if dev_hits:
        report.warn(
            "%d dev/path descriptor(s) found in env/ - these resolve to a local disk "
            "and will break on other machines:\n            %s"
            % (len(dev_hits), "\n            ".join(dev_hits[:10]))
        )

    # --- core pinning ----------------------------------------------------
    core_api = os.path.join(config_root, "core", "core_api.yml")
    if os.path.isfile(core_api):
        hits = _grep(core_api, r"^\s*version:\s*(\S+)")
        if hits:
            log("  [info]  config pins tk-core %s" % hits[0][1].split(":", 1)[1].strip())


def validate_zip(zip_path, report):
    with zipfile.ZipFile(zip_path, "r") as archive:
        names = archive.namelist()
        bad = archive.testzip()
        if bad is not None:
            report.error("zip is corrupt at entry %s" % bad)
    roots = set(name.split("/")[0] for name in names if "/" in name)
    if "core" not in roots:
        report.error("zip has no core/ at its root (entries: %s)" % ", ".join(sorted(roots)[:8]))
    size = os.path.getsize(zip_path)
    if size > LARGE_ZIP_BYTES:
        report.warn(
            "zip is %.1f MB - large configs slow down every artist's first bootstrap"
            % (size / 1024.0 / 1024.0)
        )
    return len(names), size


# --------------------------------------------------------------------------
# descriptor URI helpers (mirrors tank/descriptor/io_descriptor/base.py)
# --------------------------------------------------------------------------

def uri_to_dict(uri):
    parsed = urlparse(uri)
    if parsed.scheme != "sgtk":
        raise PublishError("Invalid descriptor uri '%s' - must begin with 'sgtk'" % uri)
    split_path = parsed.path.split(":")
    if len(split_path) != 2 or split_path[0] != "descriptor":
        raise PublishError("Invalid descriptor uri '%s' - must begin with sgtk:descriptor" % uri)
    result = {"type": split_path[1]}
    for key, values in parse_qs(parsed.query).items():
        if len(values) > 1:
            raise PublishError("Invalid descriptor uri '%s' - duplicate parameters" % uri)
        result[key] = values[0]
    return result


def dict_to_uri(descriptor_dict):
    if "type" not in descriptor_dict:
        raise PublishError("Cannot build a descriptor uri without a type: %s" % descriptor_dict)
    chunks = []
    for key in sorted(descriptor_dict):
        if key == "type":
            continue
        # tk-core uses safe="@:\\" here, which percent-encodes '/' even though its
        # own comment says not to. We keep '/' literal: both forms parse to the
        # identical dict (parse_qs unquotes), it matches how descriptors are
        # actually written by hand in the PTR field, and it keeps the audit trail
        # on that field to a one-token diff per release instead of a full re-encode.
        chunks.append("%s=%s" % (key, quote(str(descriptor_dict[key]), safe="/@:\\")))
    return "sgtk:descriptor:%s?%s" % (descriptor_dict["type"], "&".join(chunks))


def build_git_descriptor(existing_uri, git_url, version, descriptor_type, branch):
    """Return the descriptor uri for ``version``, preserving unknown keys."""
    if existing_uri:
        data = uri_to_dict(existing_uri)
        if data.get("type") not in ("git", "git_branch"):
            raise PublishError(
                "Existing descriptor is of type '%s', not a git descriptor: %s\n"
                "Pass --git-url to replace it outright." % (data.get("type"), existing_uri)
            )
    else:
        if not git_url:
            raise PublishError(
                "No descriptor set on the pipeline configuration and no --git-url given."
            )
        data = {"type": descriptor_type or "git"}

    if git_url:
        data["path"] = git_url
    if descriptor_type:
        data["type"] = descriptor_type
    if data["type"] == "git_branch":
        if branch:
            data["branch"] = branch
        if not data.get("branch"):
            raise PublishError("git_branch descriptors need --branch")
    else:
        data.pop("branch", None)
    data["version"] = version
    return dict_to_uri(data)


# --------------------------------------------------------------------------
# Flow Production Tracking
# --------------------------------------------------------------------------

def import_shotgun_api(tk_core_path=None):
    if tk_core_path:
        candidate = os.path.join(tk_core_path, "python")
        if os.path.isdir(candidate):
            sys.path.insert(0, candidate)
        sys.path.insert(0, tk_core_path)
    try:
        import shotgun_api3
    except ImportError:
        raise PublishError(
            "shotgun_api3 is not importable.\n"
            "  online : pip install shotgun-api3\n"
            "  offline: pip install --no-index --find-links <wheelhouse> shotgun-api3\n"
            "           or point --tk-core at a tk-core install that vendors it."
        )
    return shotgun_api3


def connect(args):
    shotgun_api3 = import_shotgun_api(args.tk_core)
    site = args.site or os.environ.get("SG_SITE") or os.environ.get("SHOTGUN_SITE")
    if not site:
        raise PublishError("No PTR site given. Use --site or set SG_SITE.")
    site = site.rstrip("/")

    script_name = args.script_name or os.environ.get("SG_SCRIPT_NAME")
    api_key = args.api_key or os.environ.get("SG_API_KEY")
    login = args.login or os.environ.get("SG_LOGIN")
    password = args.password or os.environ.get("SG_PASSWORD")

    if script_name and api_key:
        debug("authenticating as script '%s'" % script_name)
        credentials = dict(script_name=script_name, api_key=api_key)
    elif login and password:
        debug("authenticating as user '%s'" % login)
        credentials = dict(login=login, password=password)
    else:
        raise PublishError(
            "No credentials. Provide SG_SCRIPT_NAME + SG_API_KEY (recommended) "
            "or SG_LOGIN + SG_PASSWORD."
        )

    # shotgun_api3.Shotgun() connects during construction, so both it and the
    # explicit connect() need to be inside the same guard.
    try:
        connection = shotgun_api3.Shotgun(site, **credentials)
        connection.connect()
    except Exception as error:  # noqa: BLE001 - transport errors vary wildly
        raise PublishError(
            "Could not connect to %s: %s\n"
            "  check the site url, the script user name/key, and that this machine "
            "is allowed to reach the site." % (site, error)
        )
    log("  connected to %s" % site)
    return connection


def readable_pc_fields(connection):
    """Intersect the fields we want with what the site actually has."""
    schema = connection.schema_field_read(PC_ENTITY)
    wanted = [
        "id",
        "code",
        "project",
        "plugin_ids",
        "sg_plugin_ids",
        DESCRIPTOR_FIELD,
        "sg_descriptor",
        UPLOAD_FIELD,
        "sg_uploaded_config",
    ] + list(PATH_FIELDS)
    fields = [name for name in wanted if name in schema]
    if UPLOAD_FIELD not in schema:
        raise PublishError(
            "This PTR site has no PipelineConfiguration.%s field, which distributed "
            "configs require." % UPLOAD_FIELD
        )
    return fields


def find_pipeline_configs(connection, args, fields):
    if args.pipeline_config_id:
        filters = [["id", "in", list(args.pipeline_config_id)]]
    else:
        names = args.config_name or [DEFAULT_CONFIG_NAME]
        filters = [["code", "in", names]]
        if args.site_wide:
            filters.append(["project", "is", None])
        elif args.project_id:
            filters.append(["project", "is", {"type": "Project", "id": args.project_id}])
        elif not args.all_projects:
            raise PublishError(
                "Specify a target: --pipeline-config-id, or --project-id, "
                "or --site-wide, or --all-projects."
            )
    found = connection.find(PC_ENTITY, filters, fields, order=[{"field_name": "id", "direction": "asc"}])
    if not found:
        raise PublishError("No Pipeline Configuration matched %s" % filters)
    return found


def create_pipeline_config(connection, args, fields):
    if not args.project_id:
        raise PublishError("--create-if-missing needs --project-id")
    name = (args.config_name or [DEFAULT_CONFIG_NAME])[0]
    data = {
        "code": name,
        "project": {"type": "Project", "id": args.project_id},
        "plugin_ids": args.plugin_ids or DEFAULT_PLUGIN_IDS,
    }
    log("  creating Pipeline Configuration '%s' on project %s" % (name, args.project_id))
    created = connection.create(PC_ENTITY, data, return_fields=fields)
    return [created]


def describe_pc(pc):
    project = pc.get("project") or {}
    return "%s (id %s%s)" % (
        pc.get("code"),
        pc.get("id"),
        ", project %s" % project.get("name") if project else ", site-wide",
    )


def inspect_precedence(pc, mode):
    """Check the target against the resolver's field precedence.

    Returns ``(blocker_code, blocker_message, notes)``. ``blocker_code`` is one
    of ``None``, ``"centralized"`` or ``"descriptor_wins"``.
    """
    notes = []
    paths = {f: pc.get(f) for f in PATH_FIELDS if pc.get(f)}
    plugin_ids = pc.get("plugin_ids") or pc.get("sg_plugin_ids")
    descriptor = pc.get(DESCRIPTOR_FIELD) or pc.get("sg_descriptor")
    uploaded = pc.get(UPLOAD_FIELD) or pc.get("sg_uploaded_config")

    if paths:
        return (
            "centralized",
            "this is a centralized config - %s is set, and path fields win over both "
            "descriptor and %s. Nothing published here would be used."
            % (", ".join(sorted(paths)), UPLOAD_FIELD),
            notes,
        )
    if not plugin_ids:
        notes.append(
            "plugin_ids is empty; distributed configs need it set (e.g. '%s') or "
            "bootstrap will reject this config. Use --set-plugin-ids." % DEFAULT_PLUGIN_IDS
        )
    if mode == "upload" and descriptor:
        return (
            "descriptor_wins",
            "the descriptor field is set (%s) and takes precedence over %s, so the "
            "upload would be ignored. Re-run with --clear-descriptor to switch this "
            "config to uploaded-zip mode." % (descriptor, UPLOAD_FIELD),
            notes,
        )
    if mode == "descriptor" and uploaded:
        notes.append(
            "an uploaded config is also present; the descriptor wins so the attachment "
            "is dead weight. Use --clear-uploaded-config to remove it."
        )
    return None, None, notes


def upload_with_retry(connection, pc_id, zip_path, display_name, retries=3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            return connection.upload(
                PC_ENTITY,
                pc_id,
                zip_path,
                field_name=UPLOAD_FIELD,
                display_name=display_name,
            )
        except Exception as error:  # noqa: BLE001 - network errors vary by transport
            last = error
            if attempt == retries:
                break
            wait = 2 ** attempt
            log("  upload attempt %d/%d failed (%s), retrying in %ds"
                % (attempt, retries, error, wait))
            time.sleep(wait)
    raise PublishError("Upload failed after %d attempts: %s" % (retries, last))


# --------------------------------------------------------------------------
# GitHub Actions integration
# --------------------------------------------------------------------------

def gha_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as handle:
        handle.write("%s=%s\n" % (key, value))


def gha_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as handle:
        handle.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="publish_config.py",
        description="Build a Toolkit config zip from git and publish it to Flow Production Tracking.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # CI: called from the release workflow
  publish_config.py --ref "$TAG" --version "$TAG" --project-id 881 --config-name Primary

  # manual, offline studio: publish the v2.1.0 tag from a local clone
  publish_config.py --ref v2.1.0 --version v2.1.0 --project-id 881 --clear-descriptor

  # build the artifact only, no network at all
  publish_config.py --ref v2.1.0 --version v2.1.0 --zip-only --output ./dist/config.zip

  # upload an artifact built elsewhere (or a GitHub source zipball)
  publish_config.py --zip ./dist/config.zip --version v2.1.0 --pipeline-config-id 925

  # keep the git descriptor workflow, just move it to the new tag
  publish_config.py --mode descriptor --version v2.1.0 --project-id 881
""",
    )

    source = parser.add_argument_group("source")
    source.add_argument("--source-dir", default=os.getcwd(),
                        help="repo or config folder to publish from (default: cwd)")
    source.add_argument("--ref", help="git ref/tag to export; omit to use the working tree")
    source.add_argument("--config-subdir",
                        help="path to the config root inside the repo (default: auto-detect)")
    source.add_argument("--zip", dest="zip_path",
                        help="publish this existing zip instead of building one "
                             "(a GitHub source zipball is re-rooted automatically)")
    source.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                        help="extra exclude pattern (repeatable)")
    source.add_argument("--no-default-excludes", action="store_true")
    source.add_argument("--strip-installed-state", action="store_true",
                        help="drop core/pipeline_configuration.yml and core/install_location.yml")
    source.add_argument("--no-stamp", action="store_true",
                        help="do not write the %s provenance file" % DEFAULT_STAMP_FILE)
    source.add_argument("--stamp-file", default=DEFAULT_STAMP_FILE)
    source.add_argument("--output", help="where to write the built zip (default: a temp file)")

    checks = parser.add_argument_group("validation")
    checks.add_argument("--strict", action="store_true", help="treat warnings as errors")
    checks.add_argument("--skip-validation", action="store_true")

    site = parser.add_argument_group("site + credentials")
    site.add_argument("--site", help="PTR site url (env SG_SITE)")
    site.add_argument("--script-name", help="script user name (env SG_SCRIPT_NAME)")
    site.add_argument("--api-key", help="script user key (env SG_API_KEY)")
    site.add_argument("--login", help="human login, alternative to a script user (env SG_LOGIN)")
    site.add_argument("--password", help="password for --login (env SG_PASSWORD)")
    site.add_argument("--tk-core", help="path to a tk-core install to import shotgun_api3 from")

    target = parser.add_argument_group("target pipeline configuration")
    target.add_argument("--project-id", type=int, help="Project entity id")
    target.add_argument("--config-name", action="append", default=[],
                        help="Pipeline Configuration name (repeatable, default: %s)" % DEFAULT_CONFIG_NAME)
    target.add_argument("--pipeline-config-id", action="append", type=int, default=[],
                        help="target by id (repeatable); overrides name/project lookup")
    target.add_argument("--site-wide", action="store_true",
                        help="target configs with no project link")
    target.add_argument("--all-projects", action="store_true",
                        help="target every project's config with the given name")
    target.add_argument("--create-if-missing", action="store_true")

    action = parser.add_argument_group("what to publish")
    action.add_argument("--mode", choices=["upload", "descriptor"], default="upload")
    action.add_argument("--version", help="release tag, e.g. v2.1.0 (required for --mode descriptor)")
    action.add_argument("--git-url", help="repo url for the descriptor's path= parameter")
    action.add_argument("--descriptor-type", choices=["git", "git_branch"])
    action.add_argument("--branch", help="branch for git_branch descriptors")
    action.add_argument("--clear-descriptor", action="store_true",
                        help="blank the descriptor field so the uploaded zip takes effect")
    action.add_argument("--clear-uploaded-config", action="store_true",
                        help="blank the uploaded_config field when publishing a descriptor")
    action.add_argument("--set-plugin-ids", action="store_true",
                        help="set plugin_ids to '%s' when empty" % DEFAULT_PLUGIN_IDS)
    action.add_argument("--plugin-ids", help="explicit plugin_ids value for --set-plugin-ids/--create-if-missing")
    action.add_argument("--force", action="store_true",
                        help="publish even when field precedence says it will be ignored")

    run = parser.add_argument_group("run")
    run.add_argument("--zip-only", action="store_true", help="build and validate, never touch the network")
    run.add_argument("--dry-run", action="store_true", help="connect and report, but change nothing")
    run.add_argument("--verbose", "-v", action="store_true")
    return parser


# --------------------------------------------------------------------------
# main flow
# --------------------------------------------------------------------------

def prepare_payload(args, workdir):
    """Stage the config, validate it, and return (zip_path, meta, report)."""
    staging = os.path.join(workdir, "staged")
    os.makedirs(staging)
    patterns = ([] if args.no_default_excludes else list(DEFAULT_EXCLUDES)) + list(args.exclude)

    meta = {"version": args.version}

    if args.zip_path:
        step("Re-rooting %s" % args.zip_path)
        if not os.path.isfile(args.zip_path):
            raise PublishError("--zip file not found: %s" % args.zip_path)
        unpacked = os.path.join(workdir, "unpacked")
        os.makedirs(unpacked)
        extract_zip(args.zip_path, unpacked)
        config_root = find_config_root(unpacked, args.config_subdir, allow_wrapper=True)
        copy_working_tree(config_root, staging, patterns)
        meta["source"] = os.path.basename(args.zip_path)
    else:
        source = os.path.abspath(args.source_dir)
        repo = git_repo_root(source)
        if args.ref:
            if not repo:
                raise PublishError("--ref given but %s is not inside a git repo" % source)
            step("Exporting %s from %s" % (args.ref, repo))
            exported = os.path.join(workdir, "export")
            os.makedirs(exported)
            export_git_ref(repo, args.ref, exported)
            sha, described = git_describe(repo, args.ref)
            meta.update({"commit": sha, "describe": described,
                         "repository": git_remote_url(repo), "ref": args.ref})
            # honour --source-dir pointing at a subfolder of the repo
            rel = os.path.relpath(source, repo).replace(os.sep, "/")
            base = exported if rel in (".", "") else os.path.join(exported, rel.replace("/", os.sep))
            config_root = find_config_root(base, args.config_subdir)
        else:
            step("Staging the working tree at %s" % source)
            config_root = find_config_root(source, args.config_subdir)
            if repo:
                sha, described = git_describe(repo, "HEAD")
                meta.update({"commit": sha, "describe": described,
                             "repository": git_remote_url(repo), "ref": "HEAD"})
                if git_is_dirty(repo):
                    log("  [warn]  working tree has uncommitted changes - "
                        "publishing something that is not in git")
                    meta["dirty"] = "true"
        copy_working_tree(config_root, staging, patterns)

    prune(staging, patterns)

    if args.strip_installed_state:
        for rel in INSTALLED_STATE_FILES:
            victim = os.path.join(staging, rel.replace("/", os.sep))
            if os.path.isfile(victim):
                os.remove(victim)
                log("  stripped %s" % rel)

    if not args.no_stamp:
        meta.setdefault("built_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        meta.setdefault("built_by", os.environ.get("GITHUB_ACTOR") or os.environ.get("USERNAME")
                        or os.environ.get("USER"))
        meta.setdefault("workflow_run", os.environ.get("GITHUB_RUN_ID"))
        write_stamp(staging, args.stamp_file, meta)

    step("Inspecting the configuration")
    report = Report()
    if args.skip_validation:
        log("  skipped (--skip-validation)")
    else:
        validate_config(staging, report)

    label = (args.config_name or [DEFAULT_CONFIG_NAME])[0].lower().replace(" ", "_")
    zip_name = "%s-%s.zip" % (label, args.version or meta.get("describe") or "config")
    zip_path = os.path.abspath(args.output) if args.output else os.path.join(workdir, zip_name)

    step("Building %s" % zip_path)
    count = make_zip(staging, zip_path)
    entries, size = validate_zip(zip_path, report)
    log("  %d files, %d entries, %.2f MB" % (count, entries, size / 1024.0 / 1024.0))

    return zip_path, meta, report


def publish(args, connection, zip_path, meta):
    fields = readable_pc_fields(connection)
    try:
        targets = find_pipeline_configs(connection, args, fields)
    except PublishError:
        if not args.create_if_missing:
            raise
        targets = create_pipeline_config(connection, args, fields)

    log("  %d target(s): %s" % (len(targets), ", ".join(describe_pc(pc) for pc in targets)))

    results = []
    for pc in targets:
        step("Publishing to %s" % describe_pc(pc))
        code, blocker, notes = inspect_precedence(pc, args.mode)
        for note in notes:
            log("  [warn]  %s" % note)

        # --clear-descriptor resolves the descriptor-wins conflict by blanking
        # the field in the same update, so it is not a blocker in that case.
        resolved_here = code == "descriptor_wins" and args.clear_descriptor
        if code and not resolved_here:
            if not args.force:
                raise PublishError("%s\n  (--force overrides this check)" % blocker)
            log("  [warn]  %s -- proceeding because of --force" % blocker)

        updates = {}
        if args.set_plugin_ids and not (pc.get("plugin_ids") or pc.get("sg_plugin_ids")):
            updates["plugin_ids"] = args.plugin_ids or DEFAULT_PLUGIN_IDS

        if args.mode == "upload":
            if args.clear_descriptor and pc.get(DESCRIPTOR_FIELD):
                updates[DESCRIPTOR_FIELD] = None
            display_name = os.path.basename(zip_path)
            if args.dry_run:
                log("  [dry-run] would upload %s to %s.%s" % (display_name, PC_ENTITY, UPLOAD_FIELD))
                if updates:
                    log("  [dry-run] would update %s" % updates)
                results.append((pc, None, None))
                continue
            if updates:
                connection.update(PC_ENTITY, pc["id"], updates)
                log("  updated %s" % ", ".join(sorted(updates)))
            attachment_id = upload_with_retry(connection, pc["id"], zip_path, display_name)
            log("  uploaded -> Attachment %s" % attachment_id)
            resolved = dict_to_uri({
                "type": "shotgun",
                "entity_type": PC_ENTITY,
                "id": pc["id"],
                "field": UPLOAD_FIELD,
                "version": attachment_id,
            })
            log("  clients will bootstrap: %s" % resolved)
            results.append((pc, attachment_id, resolved))
        else:
            if not args.version:
                raise PublishError("--mode descriptor requires --version")
            uri = build_git_descriptor(
                pc.get(DESCRIPTOR_FIELD) or pc.get("sg_descriptor"),
                args.git_url or meta.get("repository"),
                args.version,
                args.descriptor_type,
                args.branch,
            )
            updates[DESCRIPTOR_FIELD] = uri
            if args.clear_uploaded_config and pc.get(UPLOAD_FIELD):
                updates[UPLOAD_FIELD] = None
            if args.dry_run:
                log("  [dry-run] would set %s" % updates)
                results.append((pc, None, uri))
                continue
            connection.update(PC_ENTITY, pc["id"], updates)
            log("  descriptor -> %s" % uri)
            results.append((pc, None, uri))

    return results


def main(argv=None):
    global _VERBOSE
    parser = build_parser()
    args = parser.parse_args(argv)
    _VERBOSE = args.verbose

    if args.mode == "descriptor" and args.zip_path:
        parser.error("--zip makes no sense with --mode descriptor")
    if args.mode == "descriptor" and args.zip_only:
        parser.error("--zip-only has nothing to build in descriptor mode")
    if args.all_projects and args.pipeline_config_id:
        parser.error("--all-projects and --pipeline-config-id are mutually exclusive")

    workdir = tempfile.mkdtemp(prefix="fptr-publish-")
    zip_path = None
    try:
        if args.mode == "upload":
            zip_path, meta, report = prepare_payload(args, workdir)
            step("Validation")
            if report.emit(args.strict):
                log("\nValidation failed. Fix the issues above, or re-run with "
                    "--skip-validation to publish anyway.")
                return 2
            gha_output("zip_path", zip_path)
        else:
            zip_path, meta = None, {}
            source = os.path.abspath(args.source_dir)
            repo = git_repo_root(source)
            if repo:
                meta["repository"] = git_remote_url(repo)

        if args.zip_only:
            step("Done (--zip-only)")
            log("  artifact: %s" % zip_path)
            log("  upload it later with: publish_config.py --zip %s --version %s ..."
                % (zip_path, args.version or "<tag>"))
            if args.output:
                return 0
            log("  [warn]  no --output given, the artifact is in a temp folder that "
                "is about to be deleted")
            return 0

        step("Connecting to Flow Production Tracking")
        connection = connect(args)
        results = publish(args, connection, zip_path, meta)

        step("Summary")
        summary = ["| Pipeline Configuration | Result |", "| --- | --- |"]
        for pc, attachment_id, uri in results:
            outcome = "dry-run" if args.dry_run else (
                "attachment %s" % attachment_id if attachment_id else uri
            )
            log("  %-40s %s" % (describe_pc(pc), outcome))
            summary.append("| %s | `%s` |" % (describe_pc(pc), outcome))
            if attachment_id:
                gha_output("attachment_id", attachment_id)
            if uri:
                gha_output("descriptor", uri)
        gha_summary(["### Config published to Flow Production Tracking", ""] + summary)
        return 0

    except PublishError as error:
        log("\nERROR: %s" % error)
        return 1
    except KeyboardInterrupt:
        log("\nAborted.")
        return 1
    except Exception as error:  # noqa: BLE001 - a stack trace helps nobody at 2am
        log("\nERROR: unexpected failure talking to the site: %s: %s"
            % (type(error).__name__, error))
        if _VERBOSE:
            import traceback
            traceback.print_exc()
        else:
            log("  re-run with --verbose for the full traceback.")
        return 1
    finally:
        # only the temp workdir is disposable; an explicit --output lives outside it
        rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
