"""The registry importer: one adapter module per registry format, and the
pipeline they share (licence, compose conversion, checker, settings; it
writes the entries). build/import_registry.py is the entrypoint, and its
ADAPTERS names each adapter by the --format that picks it.
"""
