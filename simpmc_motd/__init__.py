"""Core services for the SimpMC-Motd AstrBot plugin.

The AstrBot entrypoint intentionally stays in :mod:`main`.  Keeping this package
free of entrypoint imports prevents duplicate handler registration during hot
reloads and makes the core logic testable without an AstrBot runtime.
"""
