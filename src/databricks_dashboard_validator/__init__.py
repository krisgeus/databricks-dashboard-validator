"""Extract the inline SQL from Databricks Lakeview dashboards and lint it with sqlfluff."""

from databricks_dashboard_validator.extract import Snippet, extract_snippets

__all__ = ["Snippet", "extract_snippets"]
