"""Databricks source: incremental reads of Delta tables via their change data feed.

Only ``local_delta.py`` may import ``deltalake`` or ``pyarrow``; everything else talks to
the ``DeltaTableClient`` protocol, so a Databricks SQL client can replace it later.
"""
