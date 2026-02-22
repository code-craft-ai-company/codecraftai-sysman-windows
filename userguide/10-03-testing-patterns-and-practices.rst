.. _10-03-testing-patterns-and-practices:

==============================
Testing Patterns and Practices
==============================

Testing is a crucial part of software development that ensures the reliability and
correctness of code. This section outlines best practices and patterns for writing
effective tests.


Test Granularity
================
Tests should be written to verify the functionality of individual components or
functions in isolation. Each test should focus on a single aspect of the code to
ensure clarity and maintainability.

Tests should NOT serve as a composite test for multiple components or functions where
a sigle test verifies the functionality of multiple components or functions in a series
of assertions.  This practice does not provide adequate isolation and causes checks to be
missed when an assertion fails. Instead, each test should be focused on a single
component or function, allowing for easier debugging and understanding of test failures.

The unittest framework in Python provides setup and tearDown methods that can be
used to establish common conditions that can be shared across multiple tests within
a given test class.  This allows for efficient code reuse while maintaining seperation
of concerns for individual tests.


Unit Tests
==========


TestBase Tests
==============

A 'testbase' test takes the form of a function with a 'test_' prefix like so:

.. code-block:: python

    def test_something():
        return


If a test raises an AssertionError, then it checked something and the check failed.  That is a Failure.  If the test raises any other exception, then the test framework treats that as an Error much like the 'unittest' framework does.

You can create resource factories.

.. code-block:: python

    @testbase.register.resource_factory()
    def create_landscape(constraints={}) -> Generator[Landscape, None, None]:
        """
            Creates the global :class:`Landscape` instance.

            :param constraints: Optional dictionary of constraints used while constructing the Landscape.

            :returns: A generator yielding the Landscape object.
        """
        lscape = LandscapeSingleton()
        yield lscape


You can create resources using the factories in a scope in the test tree.  Resources have scope for every test below where they are declared.

.. code-block:: python

    testbase.originate.parameter(create_landscape, identifier='lscape')


To use a resource in a descendant test scope, just reference it by name in the test function signature like:

.. code-block:: python

    def test_use_landscape(lscape: Landscape):

        blah = lscape.get_something()
        testbase.assert_equal(blah is not None, True)
        return


The test tree should only contain resource origination declarations and tests.  It should not contain factories unless the usage of the factory is very isolated.  It is preferable to place factories in a folder such as a factory that creates the AutomationPodSimple object might be located at 'source/testroots/automojo/testshared/factories/automationpods/automationpodsimple.py'

# Evaluating Test Results
There is a machine processable JSOS stream file in the output folder.

"*outputfolder*/testrun_results_stream.jsos"

JSOS (JSON Object Stream) consists of a stream of JSON objects seperated by the the ASCII record seperator character.  When a
test starts it writes a preview to the stream.  The test preview has a 'result' equal to 'UNSET'.  Example Preview:

.. code-block:: javascript

    {
        "name": "automojo.tests.singlehost.interop.protocols.dns.test_dnsaddress#test_write_passes_address",
        "monikers": [],
        "pivots": {},
        "instance": "abb868e4-ce97-4bae-b0af-715da0d61d92",
        "parent": "5aad244e-41dc-438f-97a6-bd8365cccdd2",
        "rtype": "TEST",
        "result": "UNSET",
        "start": "2025-07-15T22:07:03.275239",
        "stop": ""
    }


When a test finishes it writes a result to the stream.  A test result will always have a 'detail' attribute. Example Result:

.. code-block:: javascript
    {
        "name": "automojo.tests.singlehost.interop.protocols.dns.test_dnsaddress#test_write_passes_address",
        "monikers": [],
        "pivots": {},
        "instance": "abb868e4-ce97-4bae-b0af-715da0d61d92",
        "parent": "5aad244e-41dc-438f-97a6-bd8365cccdd2",
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


