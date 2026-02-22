.. _10-04-03-testing-frameworks-testbase:

Testing Frameworks - TestBase and TestPlus
==========================================

.. contents:: Outline
   :local:
   :depth: 1

Purpose and Audience
--------------------

- Describe how this repository applies the ``testbase`` and ``testplus`` frameworks so contributors and AutomationX can create, organize, and run scenarios consistently.
- Clarify when to choose ``testbase`` (single-host or self-contained integration tests) versus ``testplus`` (multi-host or clustered resources).
- Complement :doc:`10-03-testing-patterns-and-practices` by focusing on framework-specific conventions, directory layout, and result artifacts.

Test Roots and Resource Layout
------------------------------

- Test suites live under ``<repository>/source/testroots/testbase`` or, for organizations with namespace customized code, ``<repository>/source/testroots/<org namespace root>``.
- Every ``testbase`` test root requires a ``__testroot__.py`` file that declares the root type as ``testbase`` so discovery utilities can register it correctly.
- Organize tests by resource configuration (for example ``source/testroots/automojo/tests/singlehost`` for single-machine coverage and ``source/testroots/automojo/tests/simplecluster`` for clustered tests).
- Keep directories parallel to the source hierarchy whenever possible so it remains obvious which packages each subtree validates.
- Shared factories, fixtures, or helpers belong under ``source/testroots/<root>/testshared`` (for example ``source/testroots/automojo/testshared/factories/automationpods/automationpodsimple.py``) to avoid cluttering test nodes.

Environment Preparation
-----------------------

- Execute ``repository-setup/rehome-repository`` after cloning so the expected ``.env`` file exists for both frameworks.
- Run ``development/setup-environment`` to create or refresh the virtual environment, then activate it before invoking ``testbase`` or ``testplus`` entry points.
- Inside Codex containers, start with ``development/codex-setup``; it chains the repository bootstrap steps AutomationX relies on.
- Create a dedicated output directory per run so logs, JSOS streams, and other artifacts remain isolated and easy to archive.

Writing Tests with TestBase
---------------------------

- ``testbase`` tests are plain functions prefixed with ``test_``; test discovery mirrors ``pytest`` naming but assertions follow ``testbase`` utilities::

    def test_something():
        return

- Raising ``AssertionError`` marks a failure, while any other exception is treated as an error, similar to ``unittest`` semantics.
- Define reusable infrastructure via resource factories and register them with ``testbase`` so dependent scenarios can request them::

    @testbase.register.resource_factory()
    def create_landscape(constraints=None):
        """Yield the global Landscape instance respecting optional constraints."""
        constraints = constraints or {}
        lscape = LandscapeSingleton()
        yield lscape

- Originate resources in the test tree to define their scope; descendants inherit the resource automatically::

    testbase.originate.parameter(create_landscape, identifier='lscape')

- Consume resources by adding them to the function signature::

    def test_use_landscape(lscape):
        result = lscape.get_something()
        testbase.assert_equal(result is not None, True)

- Keep the test tree limited to resource origination and tests. Place factories or helper modules in shared folders so each test file stays focused on scenarios and assertions.

Working with TestPlus
---------------------

- Use ``testplus`` for suites that orchestrate multiple networked machines, clusters, or hardware resources that exceed a single host.
- Mirror the same directory and naming conventions described above so AutomationX can pivot between ``testbase`` (single host) and ``testplus`` (multi host) suites without relearning structure.
- Document any cluster-specific constraints inside the relevant ``testplus`` subdirectories, especially if human approval or provisioning is required before AutomationX can execute the suite.

Running Tests
-------------

- During AI-based testing, only the ``singlehost`` suites are typically available; more complex clusters (for example ``simplecluster``) require infrastructure that Codex sandboxes do not provide yet.
- Run a focused ``testbase`` suite using::

    source .venv/bin/activate
    testbase --root source/testroots/automojo --includes=automojo.tests.singlehost --output=<desired output folder>

- Substitute the ``--includes`` path with any other resource-specific subtree (for example ``automojo.tests.simplecluster``) when appropriate infrastructure exists.
- Always point ``--output`` to a writable directory created for the current run so artifacts do not overlap between executions.

Evaluating Test Results
-----------------------

- Every run emits a machine-processable JSOS (JSON Object Stream) file named ``testrun_results_stream.jsos`` inside the specified output folder.
- Each JSON object is separated by the ASCII record separator character; previews record the start of a test with ``"result": "UNSET"`` and completions include the final status.
- Example preview entry::

    {
        "name": "automojo.tests.singlehost.interop.protocols.dns.test_dnsaddress#test_write_passes_address",
        "rtype": "TEST",
        "result": "UNSET",
        "start": "2025-07-15T22:07:03.275239",
        "stop": ""
    }

- Example completion entry::

    {
        "name": "automojo.tests.singlehost.interop.protocols.dns.test_dnsaddress#test_write_passes_address",
        "rtype": "TEST",
        "result": "PASSED",
        "start": "2025-07-15T22:07:03.275239",
        "stop": "2025-07-15T22:07:03.276034",
        "detail": {
            "errors": [],
            "failures": [],
            "warnings": []
        }
    }

- Post-processing tools or CI jobs can tail this stream to highlight failures in real time; keep sample parsers near the test roots so contributors can reuse them.
