"""Entry point for `python -m databricks_dashboard_validator`."""

import sys

from databricks_dashboard_validator.cli import main

if __name__ == "__main__":
    sys.exit(main())
