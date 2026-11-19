"""node_ops - independent probe / speed-test / region-classify / allocation-advice
pipeline for the B part of the mihomo refactor.

The package is deliberately self-contained: it imports nothing from the existing
``scripts/`` modules (no convert_sub, sync-feishu-bitable, proxy-auto-switch,
source1/source3, scripts/lib, ...) and nothing from ``node_provider``.

Data flow (S3): node_provider extracts nodes and writes them to the Feishu
「节点测速总表」.  This package only reads that table, probes the nodes, and
writes back measurement columns plus 「有效节点」.  Feishu Bitable is the only
hand-off surface; the local filesystem is used for a lock file and a log file
only.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
