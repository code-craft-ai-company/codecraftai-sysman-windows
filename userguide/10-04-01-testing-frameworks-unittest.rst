.. _10-04-01-testing-frameworks-unittest:

Testing Frameworks - UnitTest
=============================

.. contents:: Outline
   :local:
   :depth: 1

Purpose and Audience
--------------------

- Explain how to create, organize, and run tests in this repository with the standard library ``unittest`` package.
- Provide a single reference that works for human developers and automation agents (for example, AutomationX) working inside Codex or local workspaces.
- Highlight where this document fits relative to :doc:`10-03-testing-patterns-and-practices` and other userguide references.

Test Root and Discovery
-----------------------

- All ``unittest`` test modules live under ``<repository>/source/tests`` so they remain parallel to the source hierarchy under ``source``.
- Mirror the package path you are testing. Example: ``source/packages/foo/bar.py`` is tested in ``source/tests/packages/foo/test_bar.py``.
- Name test files ``test_*.py`` and classes ``Test*`` so ``python -m unittest`` discovery picks them up automatically.
- Keep helper utilities in ``source/tests/utils`` (or a similar supporting package) and import them via absolute package paths to avoid brittle relative imports.

Environment Preparation
-----------------------

- Run ``repository-setup/rehome-repository`` once per checkout to create the required ``.env`` file with repository-relative paths.
- Run ``development/setup-environment`` in the workspace to create/refresh the virtual environment with project dependencies.
- Activate the resulting virtual environment before executing any ``python`` or ``poetry`` commands so the standard library ``unittest`` entry points pick up the correct dependencies.
- In Codex containers, execute ``development/codex-setup`` first; it chains the setup steps expected by AutomationX agents.

Writing Tests with unittest
---------------------------

- Follow :doc:`10-00-coding-standards` and :doc:`10-02-naming-conventions` for class, method, and constant names in test modules.
- Structure each test module with:

  * Imports grouped as stdlib, third-party, then project modules.
  * ``unittest.TestCase`` subclasses that group scenario-specific tests.
  * ``setUp``/``tearDown`` or the ``setUpClass``/``tearDownClass`` hooks for shared fixtures when needed for clarity.

- Prefer descriptive test method names like ``test_raises_when_input_missing`` to improve debug output.
- Use helper assertion methods (``assertEqual``, ``assertRaises``, ``assertLogs``, etc.) rather than manual ``if`` statements to maximize useful failure messages.
- Capture configuration data via fixtures or factory helpers so tests remain isolated and repeatable.
- When mocking collaborators, rely on ``unittest.mock`` objects and patchers. Document complex mocks with inline comments for readability.

Running Tests
-------------

- From the repository root, execute ``python -m unittest`` to run the full suite located under ``source/tests``.
- To run a subtree (for example while iterating on a specific package), use ``python -m unittest discover -s source/tests -p 'test_package*.py'``.
- Target a single module or class with dotted paths, e.g., ``python -m unittest source.tests.packages.foo.test_bar.TestBar``.
- Capture verbose output using ``-v`` when debugging to list every test name.
- Integrate the commands above into ``Makefile`` or CI jobs so automation stays consistent; keep this document as the canonical reference.

Debugging Failures
------------------

- Re-run failing tests with ``-v`` and, if needed, temper failures with ``-f`` to stop after the first error for faster iterations.
- Insert temporary breakpoints via ``import pdb; pdb.set_trace()`` or ``self.assertEqual`` checkpoints for non-interactive environments.
- When mocking, ensure patches target the import location used by the unit under test; mismatched patch paths are a common source of failures.
- Document recurring troubleshooting tips here as they emerge during project work to aid future contributors and AutomationX.

Extending the Test Suite
------------------------

- When adding a new source module, create the matching directory structure under ``source/tests`` immediately to keep parity obvious.
- Update any shared fixtures or factories if new dependencies or configuration files are introduced.
- If new command-line shortcuts or scripts are added for ``unittest`` runs, cross-reference them in this document and in ``README.rst``.
