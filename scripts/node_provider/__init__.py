"""node_provider - the A part of the mihomo refactor.

Two modes, one package:

  * default (provide): read the 「有效节点」 table from Feishu Bitable and
    rebuild an atomically written mihomo file-provider YAML.
  * ``--collect``: extract nodes from subscription links and/or the desktop
    apps (explicit ``--from-app`` opt-in) and fully replace the identity/config
    columns of 「节点测速总表」.  Measurement columns are reset and belong to
    node_ops.

Feishu Bitable is the only hand-off surface with node_ops: the two packages
never import each other and never share local state.  This module never talks
to a mihomo controller: no PUT, no switch, no reload, no process start/stop.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
