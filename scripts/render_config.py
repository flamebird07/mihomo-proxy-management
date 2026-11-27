#!/usr/bin/env python3
"""Switch a rendered Windows config from the subscription provider to the Feishu pool.

``install.ps1`` renders the shared template by replacing placeholders; a host
deployed from Feishu alone has no subscription URL, so the placeholder comes
out empty and mihomo would reject the http provider.  This post-processor
rewrites exactly one thing - ``proxy-providers.subscription`` - into a file
provider that reads the pool built from the Feishu master table.  Everything
else in the document is left as it was.

    python scripts/render_config.py --input C:\\mihomo\\config.yaml
    python scripts/render_config.py --input config.yaml --dry-run

An explicit ``--output`` is always written, even when the input is already in
the target shape; the default in-place repeat is left untouched.

Exit codes:
  0 rewritten (or already in pool mode; dry-run)
  2 usage / PyYAML missing
  3 input is not a mapping or has no ``subscription`` provider
  4 output write failed
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

try:  # PyYAML is mandatory: never hand-build YAML (fail closed).
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - exercised on a bare host
    yaml = None  # type: ignore

PROVIDER_NAME = "subscription"
POOL_PROVIDER_PATH = "./providers/feishu-pool.yaml"
DEFAULT_HEALTH_CHECK = {
    "enable": True,
    "url": "https://www.gstatic.com/generate_204",
    "interval": 300,
    "timeout": 5000,
    "lazy": True,
}

#: Subscription-only keys a converted file provider must not keep.  They are
#: dropped when the provider already points at the managed pool path.
_RESIDUAL_KEYS = ("url", "interval")


class RenderConfigError(Exception):
    """A rejected document; ``kind`` is a stable, secret-free class name."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _yaml():
    if yaml is None:
        raise RenderConfigError("yaml_missing")
    return yaml


def convert_document(document, *, pool_path=POOL_PROVIDER_PATH):
    """Return ``(document, changed)`` with ``subscription`` turned into a file provider.

    The input mapping is not mutated.  Only ``proxy-providers.subscription`` is
    replaced; every other key is carried over untouched.  A provider that is
    already ``type: file`` at ``pool_path`` is left in place (idempotent), but a
    leftover ``url``/``interval`` from an earlier subscription shape is removed.
    """
    if not isinstance(document, dict):
        raise RenderConfigError("not_a_mapping")
    providers = document.get("proxy-providers")
    if not isinstance(providers, dict):
        raise RenderConfigError("provider_missing")
    provider = providers.get(PROVIDER_NAME)
    if not isinstance(provider, dict):
        raise RenderConfigError("provider_missing")

    if provider.get("type") == "file" and provider.get("path") == pool_path:
        residual = [key for key in _RESIDUAL_KEYS if key in provider]
        if not residual:
            return document, False
        cleaned = {key: value for key, value in provider.items()
                   if key not in _RESIDUAL_KEYS}
        new_providers = dict(providers)
        new_providers[PROVIDER_NAME] = cleaned
        new_document = dict(document)
        new_document["proxy-providers"] = new_providers
        return new_document, True

    health_check = dict(DEFAULT_HEALTH_CHECK)
    original_health = provider.get("health-check")
    if isinstance(original_health, dict):
        health_check.update(original_health)
    replacement = {
        "type": "file",
        "path": pool_path,
        "health-check": health_check,
    }
    new_providers = dict(providers)
    new_providers[PROVIDER_NAME] = replacement
    new_document = dict(document)
    new_document["proxy-providers"] = new_providers
    return new_document, True


def convert_text(text, *, pool_path=POOL_PROVIDER_PATH):
    """Parse ``text``, convert it and dump it back as normalised YAML."""
    yaml_module = _yaml()
    try:
        document = yaml_module.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - PyYAML raises many parser types
        raise RenderConfigError("invalid_yaml") from exc
    if not isinstance(document, dict):
        raise RenderConfigError("not_a_mapping")
    document, changed = convert_document(document, pool_path=pool_path)
    rendered = yaml_module.safe_dump(
        document, allow_unicode=True, sort_keys=False, width=120
    )
    return rendered, changed


def write_atomic(path, text):
    """Write ``text`` beside ``path`` then ``os.replace`` it into place (0600)."""
    directory = os.path.dirname(os.path.abspath(path))
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".render_config.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def build_parser():
    parser = argparse.ArgumentParser(
        prog="render_config",
        description="Turn the rendered config's subscription provider into "
                    "the Feishu-pool file provider.",
    )
    parser.add_argument("--input", required=True,
                        help="rendered config.yaml (read with UTF-8, BOM tolerated)")
    parser.add_argument("--output", default=None,
                        help="output path (default: rewrite --input in place)")
    parser.add_argument("--pool-path", default=POOL_PROVIDER_PATH,
                        help=f"managed pool provider path (default: {POOL_PROVIDER_PATH})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the conversion; never write a file")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if yaml is None:
        sys.stderr.write("PyYAML is required (pip install pyyaml)\n")
        return 2

    try:
        with open(args.input, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
    except (OSError, UnicodeError) as exc:
        sys.stderr.write(f"input unreadable class={type(exc).__name__}\n")
        return 3

    try:
        rendered, changed = convert_text(text, pool_path=args.pool_path)
    except RenderConfigError as exc:
        sys.stderr.write(f"render_config rejected input class={exc.kind}\n")
        return 3

    print(f"pool provider: type=file path={args.pool_path} changed={changed}")
    if args.dry_run:
        return 0
    # An explicit --output is a request to produce that file, even when the
    # input is already in the target shape; only the default in-place repeat
    # is a no-op that must not rewrite (and must not touch mtime/perms).
    if not changed and args.output is None:
        return 0

    output = args.output or args.input
    parent = os.path.dirname(os.path.abspath(output))
    if not os.path.isdir(parent):
        sys.stderr.write("output directory does not exist\n")
        return 4
    try:
        write_atomic(output, rendered)
    except OSError as exc:
        sys.stderr.write(f"output write failed class={type(exc).__name__}\n")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
