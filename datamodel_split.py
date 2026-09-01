"""Compatibility entry point for the former XML fragment assembler.

DataModel sources are now Peach DSL modules. New code should import
``datamodel_dsl`` directly.
"""

from datamodel_dsl import *  # noqa: F401,F403
from datamodel_dsl import main


if __name__ == "__main__":
    raise SystemExit(main())
