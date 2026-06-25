"""Execution helpers.

The one-shot ``custom_analysis_execute`` notebook engine was removed in the
single-engine consolidation (ADR 0032): the Autonomous Code Mini-Agent
(``omicsclaw.autonomous``) is now the only autonomous engine. This package now
holds just the job ``executors`` subpackage used by the remote/runtime layers.
"""
