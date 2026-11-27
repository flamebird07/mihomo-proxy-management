"""Deploy kit: bootstrap a fresh machine from a checkout with Feishu only.

Host-neutral building blocks shared by the macOS and the Windows bootstrap.
The user supplies two things - the Bitable address and an app with read/write
access to it - and everything else (mihomo, geo data, the node pool) is
derived.  Credentials enter through the environment, stdin JSON or a hidden
prompt and never appear in argv, logs or exception text.

Submodules are imported explicitly (``from deploy import credentials``); the
package ``__init__`` imports nothing so each module can stand alone.
``verify`` / ``registry`` are the only modules that reach into the pipeline
package (``node_provider``) - for its Feishu client and table helpers.
"""

__all__ = ["credentials", "feishu_url", "registry", "secrets_file", "verify"]
