"""Kernel package — the unchanging core of the personal assistant.

This package exposes the small set of deep modules listed in
``kernel/RUNTIME.md``: vault I/O, audit log, claude runner, telegram
bridge, and orchestrator. Per the project's cardinal rule, this kernel
is **never modified** to add a new use case — new use cases are plugins
under ``domains/``.
"""
