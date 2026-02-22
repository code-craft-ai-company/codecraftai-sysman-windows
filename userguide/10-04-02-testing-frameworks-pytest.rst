Testing Frameworks - PyTest
===========================

.. contents:: Outline
   :local:
   :depth: 1

Purpose and Scope
-----------------

- Describe how contributors and AutomationX agents apply ``pytest`` inside this repository while staying aligned with :doc:`10-03-testing-patterns-and-practices`.
- Provide a quick-start outline covering environment setup, directory layout, authoring guidance, and execution commands.
- Call out places where ``pytest`` differs from the ``unittest`` conventions documented in :doc:`10-04-01-testing-frameworks-unittest`.

Test Root and File Layout
-------------------------

- All ``pytest`` suites live in ``<repository>/source/testroots/pytest``; mirror the project package structure that resides under ``source``.
- Follow the same naming conventions described in :doc:`10-02-naming-conventions`: use ``test_*.py`` modules, ``Test*`` classes when applicable, and descriptive function names.
- Store shared fixtures/helpers under ``source/testroots/pytest/conftest.py`` or nested ``conftest.py`` files near the tests that need them; keep reusable utilities in dedicated helper modules to avoid circular imports.
- Keep the test hierarchy synchronized with new source modules so discovery remains predictable for tooling and CI.

Environment Preparation
-----------------------

- Execute ``repository-setup/rehome-repository`` and ``development/setup-environment`` after cloning to ensure the virtual environment and ``.env`` file exist.
- Activate the virtual environment before running ``pytest`` so plugins and dependencies resolve correctly.
- In Codex environments, run ``development/codex-setup`` first; it applies the same bootstrap flow expected locally.
- Record any additional plugins or configuration flags in ``pyproject.toml`` (``[tool.pytest.ini_options]``) so contributors share the same defaults.

Writing Tests with pytest
-------------------------

- Prefer plain test functions where they keep scenarios readable; use classes only when grouping shared fixtures or behavior.
- Leverage ``pytest`` fixtures (function, module, session scope) to express setup/teardown rather than relying on ``unittest``-style hooks.
- Keep fixtures explicit in the test signature to reinforce data flow and debuggability.
- Use built-in assertion rewriting; avoid ``self.assert*`` helpers except when wrapping complex logic in reusable helper objects.
- When mocking, rely on ``unittest.mock`` or ``pytest-mock`` (if enabled) and document any intricate patching.
- Encode parameterized scenarios with ``@pytest.mark.parametrize`` to keep related cases in a single function.

Running Tests
-------------

- Run the entire suite via ``pytest source/testroots/pytest`` from the repository root.
- Target specific packages or modules by pointing pytest to the subdirectory or file, e.g., ``pytest source/testroots/pytest/packages/foo``.
- Execute a single test using node IDs: ``pytest source/testroots/pytest/packages/foo/test_bar.py::TestBar::test_handles_edge_case``.
- Add ``-k`` expressions (``pytest -k 'edge_case and not slow'``) to filter by keyword, and ``-m`` to respect custom markers defined in ``pytest.ini``.
- Use ``-vv`` and ``--maxfail=1`` during debugging to expose detailed output and halt on the first failure.

Debugging and Tooling
---------------------

- Integrate ``pytest`` with IDE adapters or ``ptw`` (pytest-watch) when rapid feedback is useful; ensure referenced tools are documented in ``README.rst``.
- Enable ``--pdb`` or ``--trace`` to drop into interactive debugging exactly where failures occur.
- Use ``pytest --lf`` to re-run only the last failing tests or ``--ff`` to prioritize previous failures when iterating.
- Maintain a troubleshooting section in this document as recurring issues (e.g., fixture scope conflicts, plugin version mismatches) surface.

Extending the PyTest Suite
--------------------------

- Whenever new source packages appear, create the matching directory tree in ``source/testroots/pytest`` immediately.
- Register any new custom markers in ``pyproject.toml`` (``markers = [...]``) so pytest does not warn about unknown marks.
- If you add helper scripts (Make targets, ``tox`` environments, CI jobs) that wrap ``pytest``, reference them here for quick discovery.
- Keep AutomationX-focused notes (for example, sandbox-safe commands) up to date so agents can run the same workflows as human contributors.
