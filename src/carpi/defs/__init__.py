"""The car-pi definition database.

This package is *data*, not logic. Everything vehicle-specific lives here as YAML
so that adding support for a car means editing a definition file, never Python.

Layout
------
schema/          JSON Schemas, enforced in CI
generic/         standards-based definitions that apply to every OBD-II vehicle
generic/rules/   findings the report engine evaluates
vehicles/        <make>/<platform>/<ecu>.yaml -- manufacturer-specific reads

Set ``CARPI_DEFS_PATH`` to override this directory with an external checkout.
"""
