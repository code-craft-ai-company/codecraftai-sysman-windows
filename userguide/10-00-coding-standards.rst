.. _10-00-coding-standards-and-patterns:

=============================
Coding Standards and Patterns
=============================

Zen of Python testing
=====================

* It doesn't matter how beautiful the code is, beauty is in the eye of the beholder
* Readability is important
* Debuggability is paramount
* Scalability and Maintainability are critical
* Test code is an instrument for the orchestration of product code
* Flat is better than Nested
* Sparse is better than dense
* Explicit is better than implicit
* Simple is better than complex
* Bad coding patterns proliferate
* Stability is more important than performance
* Errors should never pass silently, unless purposefully handled and silenced
* If the implementation is hard to explain, follow or maintain, try thinking of another way
* If the implementation is easy to explain, follow or maintain, it may be a good idea


Semicolons
==========
Do not terminate lines of code with semi-colons, and do not use semicolons to put two statemens on the same line.

Functions
=========
* Function names should be snake case
* All functions parameters should have type hints
* Functions should have annotated return types

Methods
=======
* Method names should be snake case
* Instance methods should use 'self' as the first Argument
* Class methods should use 'cls' as the first Argument
* All method parameters should have type hints
* Methods should have annotated return types


Classes
=======
* Class names should be mixed case (PascalCase / CamelCase) and should not contain underscores
* The name of the file that contains a class should match the class name but in all lowercase, with no underscores

For example, a class named ``SomeClass`` should be defined in a file named ``someclass.py``.

.. code-block:: python

    # File: someclass.py

    class SomeClass:
        """
            This is a documentation comment for a class.
        """


Default Values for Functions or Methods
=======================================
Mutable values should not be used as a default value for a function or method parameter.  This is because
python creates a single instance for mutable defaults that it shares among all the callers of a method.  Using
a mutable default value means that modifications made to the value inside the function by one call effect all
other subsequent calls.  The following example shows how modifying a list in a function effects every call to
the function.

.. code-block:: python
    
    import random

    def add_and_print_list(items=[]):
       items.append(random.randint(1, 10))
       print(items)
       return
    
    add_and_print_list()
    add_and_print_list()

The second call to `add_and_print_list` will print the numbers added to the list from the first call.  This
is because default values to callables are only created once and shared by all function users.  When you want
to use a Mutable instance as a default value, one way to do this is to pass `None` or `...` as the default
and then create an instance for the Mutable inside of the function, for example.

.. code-block:: python

    def some_function(items: List[str] = None):

        if items is None:
            items = []

        # TODO: Do something with the list

        return


Class Variable Initialization
=============================

All variables must be initialized in the constructor of the class.

.. code-block:: python

    class Blah:
        def __init__(self):
            self._a = 0
            self._b = 0
            self._c = ""
        
        def set_to_blah(self):
            self._c = "blahblah"

This is very important for visual debuggers and tools to perform type
inferencing and popup context menues when typing and help. The tools will
inspect the constructor to see what variables are attached to **self**.
It also ensures that an initial value is present any time a variable is used.
The following is **NOT** allowed:

.. code-block:: python

    class Blah:
        def __init__(self):
            self._a = 0
            self._b = 0
        
        def set_to_blah(self):
            self._c = "blahblah"  # No initialization in constructor


Decision Expressions
====================
Shortcut 'True/False' expressions should NOT be used.  Example:

.. code-block:: python

    if somelist:
        print(somelist[0])

Shortcut expressions do not provide sufficient debugging information when a failure occurs.  There are
two error conditiions that could effect the print statement, somelist could be 'None' or it could be an
empty list.  When an expression is not-explicit error conditions get blended together.  'True/False' expressions
for decisions must be explicit for all cases.  For example:

.. code-block:: python

    if somelist is not None:
        if len(somelist) > 0:
            print(somelist[0])
        else:
           errmsg = "The parameter 'somelist' should not be empty.
           raise ValueError(errmsg)
    else:
        errmsg = "The parameter 'somelist' should not have been 'None'"
        raise ValueError(errmsg)


Decorator Usage
===============
Python decorators should only be used to:

* Add metadata to a function or method

.. code-block:: python

    @priority(1)
    def test_something():
        return

* Register a function or method with a mapping system

.. code-block:: python

    @app.route("/")
    def index():
       return

The two types of decorator uses above DO NOT modify the behavior of the wrapped function.  Python decorators
are a form of top-level code that are applied when modules are parsed and types and functions are created.
Decorators that modify the behavior of functions such as the '@retry', are locked to a function extremely early
and cannot be removed or parameterized in calls.  In this sense the decorator breaks the ability of the caller
and callable to form a contract of behavior base on parameters.

NOTE: Decorators that wrap functions and add behavior cannot be removed so the behavior they add cannot be removed.


Documentation Comments
======================

Documentation comments in this project should use the sphinx syntax of documentation comments.

All documentation comments for classes, functions and methods should:

* Be multi-line triple quote strings
* The description of the comment should start on the second line
* The text for the entire documentation comment is indented 4 spaces inside of the start position of the first triple quote
* Parameter descriptions do not need to declare a type because Type hints should be given in the function signature.
* The 'returns' description should only contain a description, the return type should be specified by a type hint
* The 'raises' description should be a list of exception types


Class Example
-------------

.. code-block:: python

    class SomeClass:
        """
            This is a documentation comment for a class.
        """


Function Example
----------------

.. code-block:: python

    def some_function(a: int, b: str) -> str:
        """
            This is a test function.

            :param a: The value for 'a'
            :param b: The value for 'b'

            :returns: The word 'blah' in a string.
            :raises: ValueError
        """
        if a <= 0:
            raise ValueError("'a' must be greater than 0")

        return "blah"


Method Example
--------------

.. code-block:: python

    class SomeClass:
        """
            This is a documentation comment for a class.
        """

        def some_method(self, a: int, b: str) -> str:
            """
                This is a test method

                :param a: The value for 'a'
                :param b: The value for 'b'

                :returns: The word 'blah' in a string.
                :raises: ValueError
            """
            if a <= 0:
                raise ValueError("'a' must be greater than 0")

            return "blah"

Code Optimization
=================

The desired characteristics of test code is different than that of production
code. Test code is often used to debug or step through scenarios and workflows
in order to capture information about what is going on in the code that is the
target of the tests. Because test code is used so often in the debugger to
perform investigations of product bugs and test issues, test code should only
be optimized where there is sufficient data to show that a specific area of code
is having a big negative impact on the performance of the test runs.

The reality of distributed automation frameworks is that they spend a lot of
time in interop APIs looping on retry attempts with delays intentionally
inserted between retries. Code that is intentionally being delayed should
**NOT** be the target of code optimizations. The following is a list of
optimization techniques that are discouraged in the test code so the test code
can exhibit more of the desired characteristics list above.

* One Liners or Excessive Brevity
* Nested Inlining Function Calls
* Compound return statements

One Liners and Brevity Shortcuts
--------------------------------

One liners and code written with the intent of being brief are not allowed and
or discouraged in the automation code. This is because ensuring the automation
code can be easily run and stepped through in the debugger is a primary requirement.
Simple list comprehensions that get assigned to a local variable are ok such as:

.. code-block:: python

    dev_list = [dev for dev in device_list]

These are acceptable because you can easily see the result of the list comprehension
in the debugger after the comprehension has run. However, if the list comprehension
becomes more complexed with if statements calling functions, then you should
break out the list creation into multiple lines like so:

.. code-block:: python

    dev_list = []
    for dev in device_list:
        if dev.deviceType == 'network/upnp':
            dev_list.append(dev)

This is most likely a little less performant. Squeezing every last bit of performance
out of the code is **NOT** a priority of creating test code that is easy to
consume, maintainable and easy to debug.

Nested Inlining Function Calls
------------------------------

An important aspect of code that is friendly to debug is that it spreads out
statements across multiple lines of code. By spreading out code statement such
as function calls or index accesses across multiple lines, we attach metadata in
the form of a line number to the statements which enables the debugger to work
more efficiently with the statements.

The following code is not debugger friendly or efficient because the statements
do not have unique line numbers associated with them in the python byte code.

.. code-block:: python

    some_function(param_function_a(), param_function_b(), param_function_c())

Another thing to keep in mind is that indexers in python are actually function calls
so statements like the ones below are also undesired in test code.

.. code-block:: python

    some_function(data[0], data[1], data[2])

A better way to get data items from a sequence or list would be to expand the sequence
to variables like so:

.. code-block:: python

    a, b, c = data
    some_function(a, b, c)

By expanding the items to individual variables, you can provide a local context of the
ordering of the variables in the list or tuple and the variables you are expanding them
to. This local context or mapping of items to variable names can be easily inspected
during a debug session to ensure that the variable name matches the data being assigned
to the variable.

Compound Return Statements
--------------------------

Avoid:

.. code-block:: python

    def some_function():
        return inner_function_call(inner_a(), inner_b(), inner_c(), inner_d())

For more details about how returns should be written, see the `Return Statements`_ section.

Passing Function Arguments
==========================

Named Parameter Passing
-----------------------
If a parameter is named, always pass the parameter to the name it is being passed as. Never pass
named parameters by position. Example:

.. code-block:: python

    def some_func(blah=None):
        return

In the function above, 'blah' is a named parameter, so in order to call this function, you should
always pass the parameter by name like so:

.. code-block:: python

    some_func(blah="Blah")

Multiline Error Message Formation
=================================

An important part of creating great automation frameworks and tools is the sharing of
expert knowledge between consumers of the automation framework code base. A great way
to implement knowledge sharing is to write code so that it provides detailed contextual
information when errors occur. This is important because the last person working or dealing
with an issue in the error handling code is working on the problem and has the best
knowledge about the context when the error occurs and should share that knowledge with others.

As part of providing well formed and detailed error reporting, we want to be able to see
and debug the code that is creating the error messages. When creating multi-line error
messages, the following method is preferred.

* Create a list to hold the error message lines
* Iterate any data collections or collect data and append lines to the list
* Create section headers for individual data sections
* Join the list of error message lines together using os.linesep.join() and assign the
  message to a variable so it can be seen in the debugger
* pass the error message variable to the exception

The code below provides an example of the building of a detailed error message that is easy to debug.

.. code-block:: python

    err_msg_lines = [
        "Failed to find expected UPNP devices after a timeout of {} seconds.".format(response_timeout)
    ]
    err_msg_lines.append("EXPECTED: ({})".format(len(expected_devices)))
    for dkey in expected_devices:
        err_msg_lines.append("    {}:".format(dkey))
    err_msg_lines.append("")
    
    err_msg_lines.append("MATCHING: ({})".format(len(scan_context.matching_devices)))
    for dkey in scan_context.matching_devices:
        err_msg_lines.append("    {}:".format(dkey))
    err_msg_lines.append("")
    
    # More data sections...
    
    err_msg = os.linesep.join(err_msg_lines)
    raise TimeoutError(err_msg) from None

Stable Property Implementations
===============================

The proper use of property is to provide controlled access to the data members of a class. The development
tools that we utilize, such as Visual Studio Code, are written with this implied behavioral
contract in mind on how properties should behave.

When we as developers break this implied contract on property behavior,
we actually cause a lot of problems for consumers of our code. That is because visual
debuggers actually rely on this implied behavior in order to provide contextual information
to the software engineer when they are running code in debug sessions.

Properties should be simple accessors to internal data and avoid:

* Running commands via SSH
* Calling methods that proxy across threads
* Blocking on synchronization primitives
* Performing complicated operations

Exception Handling
==================

The code in this project is library code, so a majority of the function, class and method implementations
should behave like a library component and release control over the resolution of exceptional conditions
to the 'Application' level of code. The code at the 'Application' level is the code that is controlling the
flow of execution. In a test run, that is the test and the test sequencing code.

Here are the rules:

1. API and library code SHOULD NOT handle an exception if handling the exception will not allow the API
   to recover and continue perform its intended purpose successfully (if you cannot fix the problem allow the exception to bubble)

2. An API or library SHOULD catch exception and reclassify them if doing so will add specific information
   about the context around which the exception was caught. The following is an example:

.. code-block:: python

    try:
        job_mod = import_by_name(job_package)
    except ImportError as imperr:
        errmsg = "Failure while importing job package %r"  % job_package
        raise argparse.ArgumentError("--job", errmsg) from imperr

3. Failure reports display the code from which an exception was raised. An API or library SHOULD catch
   and reclassify an exception if doing so will improve the code context view related to the nature of the problem
   that caused the exceptional condition. (raise exceptions from the code you want to show in the test failure reports)

4. An API or library SHOULD catch an exception and add "EXTRA" information to the exception if doing so
   will provide contextual information about unique circumstances that cause a specific exception

5. 'SemanticError' should NEVER be handled. There are raised when an API is being used improperly. This means there
   is a definitive test code failure. They should ALWAYS be allowed to bubble so the improper usage can be fixed.
   Treat one with the same severity as a compile-time error: halt and report the condition so it can be fixed, do
   not proceed under false pretences.

6. 'ConfigurationError' should NEVER be suppressed. These are raised when the code has been given incorrect
   information that it cannot rely on in order to run. The process was kicked off with wrong or corrupt input, so
   continuing would derive results from data that is already known to be untrustworthy. Detect configuration
   problems as early as possible and let them end the run. The one sanctioned pattern is aggregation that still
   terminates: a schema or spec validator MAY collect several configuration problems and raise a single
   'ConfigurationError' enumerating all of them, so an operator can fix them in one pass.

7. 'SemanticError' and 'ConfigurationError' both derive from 'BaseException' rather than 'Exception' so that they
   pass through every 'except Exception' handler and end the run no matter how much intervening code is catching
   broadly. Do NOT change those base classes. Do NOT catch, translate, or re-raise either of them as an 'Exception'
   subclass. Downgrading one into a catchable error hands a later 'except Exception' the chance to continue, and
   the subsequent error that then occurs usually carries no information about this original cause -- the real
   problem ends up hidden behind noise. The only sanctioned use of 'except SemanticError' is the guard idiom:
   catching it purely to 'raise' it again so that a broader 'except Exception' further down the same handler chain
   cannot swallow it.

8. When an API detects that a requested item is known but simply not available in the current context (a venue that
   does not offer a catalogued accelerator or image family, for example), that is a validation finding rather than
   improper use of the API. Raise an ordinary 'Exception'-derived error for it so a validator MAY aggregate it into
   a report. Reserve 'SemanticError' for the case where the requested item does not exist at all.

9. API and library code should only handle an exception under an 'Aspects' contract. The application layer is always
   the entity that decides persistence and flow of control. It MAY delegate those decisions to an API through an
   Aspects contract, but library code must never take that control away in a way that leaves the application layer
   with no say in how an exceptional condition is handled.

10. In all other cases, exceptions should be allowed to bubble

Special Exceptions
------------------

This project utilizes very specific exceptions in order to classify or categorize exceptional conditions
that occur during an automation run into specific groups which help to provide quick and immediate information
about the nature of an issue and the potential team or individual that should get involved with resolving the issue.
The groups we attempt to classify exceptional conditions into are:

* configuration issue
* test framework code misuses (semantic error)
* test failure (something was validated and found to be incorrect)

ConfigurationError
^^^^^^^^^^^^^^^^^^
It very important to detect and raise configuration exceptions. Configuration exceptions prevent noise from
being generated by the running of tests under conditions that will not result in the generation of useful test
data. Configuration exceptions also provide a chance for test framework engineers to pass on expert knowledge
to test framework consumers as to mistakes they may be making in the configuration of the test environment and
can point consumers to documentation that will help resolve the configuration issue.

SemanticError
^^^^^^^^^^^^^
Great test frameworks pass on expert knowledge to the consumers of the test framework. They do this by providing
immediate feedback to the consumer when the consumer is making a mistake or misusing a test framework API(s).

AssertionError
^^^^^^^^^^^^^^
An important feature of any great test framework is that it attempts to classify issues. An important distinction
is to identify when the test code has encountered a situation which points to an improper condition in the state
of a target under test. The 'AssertionError' is used for this purpose. Raising an 'AssertionError', or an
exception class derived from it, will result in an issue being classified as a failure of condition or state
associated with the target of the test scenario.

Return Statements
=================

All functions or methods that are not generators should have a `return` statement. The return
statements are important for four reasons:

* It prevents the formation of appended functionality during a bad code merge
* It provides line number data for the debugger
* It provides a way to check results, in context, before a return
* It makes code easier to read

Below is a detailed description of each of these issues.

Formation of Appended Functionality
-----------------------------------

One of the common tasks that is performed frequently by software developers is the refactoring or
merging of code. During the process of refactoring or merging code, function declarations might
be missed or incorrectly deleted. When this happens, new functionality can end up being inadvertently
appended to the previous function in the code. Take the following two functions as a simplified example.

.. code-block:: python

    def say_hello():
        print("Hello")
    
    def say_world():
        print("World")

If returns are not present at the end of the functions above, then during a refactor or code
merge, it is possible for lines of code to be removed, like if the `say_world` function
declaration was deleted like so:

.. code-block:: python

    def say_hello():
        print("Hello")
    
        print("World")

Now, without warning from python, the functionality of the `say_world` function has been appended
to the `say_hello` function and thus changes the functionality of the `say_hello` function without
warning.

Now lets look at what would happen if the same thing took place when return statements are utilized
as in the code below.

.. code-block:: python

    def say_hello():
        print("Hello")
        return
    
    def say_world():
        print("World")
        return

In the code above, we clearly mark the end of our functions so python has a better chance of doing
the correct thing when code is modified incorrectly. If the function declaration for `say_world`
is removed like so.

.. code-block:: python

    def say_hello():
        print("Hello")
        return

        print("World")
        return

In the case above, python will not execute the dangling code and will not append its functionality
to the `say_hello` method. Also, the python linter can show the remaining code body for `say_world`
as dead code or unreachable code and complain when it tries to lint the code in the file.

Line Number for Debugging
-------------------------
A very important aspect of test code is debuggability. In order to be able to inspect the results
of a function before it returns, you need a line of code to hang a breakpoint on. By stopping the
debugger on the return statement, you can see the values of the inputs to the function and values
of any intermediate byproducts or local variables in the context of the function.

In Context Return Verification
------------------------------

One of the most important aspects of writing debuggable code, is to write code in such a way that
you can see the values of the local variables that contributed to the creation of the value being
returned. The following is an example function that demonstrates the concept of writing functions
so the context of the return value can be examined.

.. code-block:: python

    def example_function(a: int, b: int) -> int:
        multiplier = random.randint(0, 10)
        rtnval: int = (a + b) * multiplier
        return rtnval

From looking at the simple example above, you can see that in order to debug the function and
make sure it is returning the correct answer, it is useful to be able to see the `multiplier`
parameter that is generated locally and is being used to effect the output. Providing a simple
independent return allows us to see the context that is generating the output value. Another
example below shows how a function like this might be written that will not provide the same
ability to debug the function.

.. code-block:: python

    def example_function(a: int, b: int) -> int:
        return (a + b) * random.randint(0, 10)

This function is sometimes valued by some developers for its brevity, but for testing purposes,
this coding style results in reduced quality code. The reason the code is reduced quality is
because you cannot see the context of the return value being generated.

Code Legibility
---------------

Finally, `return` statements are important to improve the legibility of code. Because python code
uses indentation to determine scope, the repeated indentation of successive code blocks can present
issues with the readability of code. This can particularly be a problem with longer functions.

For consistency and to help resolve all of these issues, It is preferred to use returns on all functions
and methods. Any function or method that is not a generator, since generators don't have returns.

InterOp and Persistence Decorators
==================================

Distributed automation scenarios engage in a lot of interop activity, and any interop activity brings
problems with reliability. A major part of distributed testing is placing reliability expectations on
interop APIs under both success and failure conditions. To highlight that: framework APIs **must** be
usable by the calling code for both **success** and **failure** conditions. They must allow the caller
to pass **valid** and **invalid** data so the behavior of the remote endpoint can be tested under
different conditions.

An experienced framework architect is aware of this important difference between framework APIs and
production APIs and designs the APIs accordingly.

Why Not a Retry Decorator
-------------------------

A pattern that is common in production code is to put a `@retry` decorator on an API that might not
have 100% reliability.

.. code-block:: python

    @retry(attempts=3, delay=5)
    def run_command(self, command: str) -> Tuple[int, str, str]:
        status, stdout, stderr = self._ssh_client.run(command)
        return status, stdout, stderr

This might work fine in a production environment where calling the API successfully is the only goal,
but for test code, interop APIs have several competing goals:

* Check expected behavior (for success and failure)
* Capture detailed data in failure contexts
* Let tests and higher level code have control of behaviors and expectations

A `@retry` decorator works against every one of these goals:

* It assumes the call should succeed. A test that expects the call to fail cannot make a single attempt
  and observe the failure, it has to wait out every retry first.
* The number of attempts and the delay are locked in when the module is imported. A test that needs to
  wait ten minutes for a host to finish an install and a test that needs a fast answer get the same
  behavior.
* It cannot express other persistence patterns, such as calling an API for as long as it keeps
  succeeding in order to detect when a service goes down.
* The failures from the earlier attempts are swallowed inside the decorator, so the details of what went
  wrong are lost to the caller.

Good framework code **does not assume** or make decisions for the code that is controlling the flow of
a test. Flow control and error handling are the responsibility of the higher level code. Good framework
code lets the higher level code **make expectations** on the behavior of the code under test and decide
what to do on success or failure based on the current context of the test.

Behavior Parameters
-------------------

A better way to design interop APIs is to let the caller pass parameters that change the behavior of the
API. The obvious way to do that is to add individual behavior parameters to the end of each API.

.. code-block:: python

    def run_cmd(self, command: str, exp_status: int = 0, repeat_until_success: bool = False,
                timeout: float = 600, interval: float = 10, log_failures: bool = True) -> Tuple[int, str, str]:

This does not scale. Each new behavior adds a parameter to every interop API, and every API that calls
another interop API has to accept and forward each of them. With hundreds of interop APIs, adding one new
behavior becomes a large change, and the parameter names and defaults drift apart from one API to the
next.

The Aspects Parameter
---------------------

Instead, the behavior modification parameters are packaged into a single object, an **aspects** object,
that is passed to the API as one parameter. The root of the aspects hierarchy is
:class:`ccai.aspects.aspects.Aspects`. Interop packages extend it with the fields they need. For example,
:class:`ccai.interop.aspects.Aspects` adds action and logging patterns, :class:`AspectsCmd` adds command
specific fields, and the REST clients add their own ``AspectsRest`` derivatives.

The following is a shortened version of the interop :class:`Aspects` class.

.. code-block:: python

    class Aspects(AspectsBase):

        def __init__(self, action_pattern: ActionPattern = ActionPattern.SINGLE_CALL,
                           completion_timeout: float = DEFAULT_COMPLETION_TIMEOUT,
                           completion_interval: float = DEFAULT_COMPLETION_INTERVAL,
                           connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT,
                           inactivity_timeout: float = DEFAULT_INACTIVITY_TIMEOUT,
                           logging_pattern: LoggingPattern = DEFAULT_LOGGING_PATTERN,
                           max_attempts: int = 1,
                           logger: Optional[logging.Logger] = None):
            ...
            self.freeze()
            return

    DEFAULT_ASPECTS = Aspects()

The most important field is ``action_pattern``, which tells the API what persistence to apply:

* ``SINGLE_CALL`` - Make exactly one attempt and return the result, success or failure, to the caller.
  This is the default.
* ``DO_UNTIL_SUCCESS`` - Repeat the call every ``completion_interval`` seconds until it succeeds. If it
  has not succeeded within ``completion_timeout`` seconds, raise a :class:`TimeoutError` that includes the
  details of the last attempt.
* ``DO_WHILE_SUCCESS`` - Repeat the call every ``completion_interval`` seconds for as long as it keeps
  succeeding. This is used to wait for something to stop, such as a service shutting down. If it is still
  succeeding after ``completion_timeout`` seconds, raise a :class:`TimeoutError`.

:class:`ActionPattern` also defines connection oriented patterns, ``SINGLE_CONNECTED_CALL`` and
``DO_UNTIL_CONNECTION_FAILURE``, that only some APIs support.

Aspects objects are frozen at the end of their constructor and cannot be modified after they are created.
This lets a single instance, including the module level defaults, be shared safely by any number of
calls. To get different behavior, create a new aspects object, do not modify an existing one.

Declaring an API That Accepts Aspects
-------------------------------------

Interop APIs that accept aspects follow these rules:

* The parameter is named ``aspects`` and it is the last parameter of the API.
* The parameter is keyword only (it follows a ``*``) so that it is always passed by name.
* The type hint is the most specific aspects class the API uses, and the default is the matching frozen
  module level default, such as ``DEFAULT_CMD_ASPECTS``.
* Every interop API takes an ``aspects`` parameter, including APIs that only read state. Reads need
  persistence and timeouts as much as writes do, and a consistent signature means callers never have to
  check which APIs support it.
* An API that calls other interop APIs passes its ``aspects`` through to them. If it drops the aspects,
  the calls it makes fall back to the defaults and the caller loses control part way down the call stack.
* The persistence loop that honors ``action_pattern`` is implemented once, in the lowest level API that
  talks to the remote endpoint, such as ``run_cmd``. Higher level APIs forward the aspects and do not add
  loops or retries of their own.
* An API that does not support a particular ``action_pattern`` raises a :class:`SemanticError` rather than
  quietly ignoring it.

An agent or client MAY accept an aspects object in its constructor to use as the default for its calls.
In that case its methods declare ``aspects: Optional[AspectsCmd] = None`` and use the instance's aspects
when ``None`` is passed. The :class:`SshAgent` works this way.

The following example shows a service agent whose APIs all take the same ``aspects`` parameter and pass
it through to ``run_cmd``, which implements the action patterns.

.. code-block:: python

    from typing import Tuple

    from ccai.interop.aspects import AspectsCmd, DEFAULT_CMD_ASPECTS
    from ccai.interop.protocols.ssh.sshagent import SshAgent

    class ServiceAgent:
        """
            Starts, stops and checks the state of services on a remote host.
        """

        def __init__(self, ssh_agent: SshAgent):
            self._ssh_agent = ssh_agent
            return

        def start_service(self, name: str, *, aspects: AspectsCmd = DEFAULT_CMD_ASPECTS) -> Tuple[int, str, str]:
            """
                Starts the service named ``name``.

                :param name: The name of the service to start.
                :param aspects: The aspects that control how the command is run.

                :returns: The status, stdout and stderr of the start command.
            """
            command = "sudo systemctl start {}".format(name)
            status, stdout, stderr = self._ssh_agent.run_cmd(command, aspects=aspects)
            return status, stdout, stderr

        def stop_service(self, name: str, *, aspects: AspectsCmd = DEFAULT_CMD_ASPECTS) -> Tuple[int, str, str]:
            """
                Stops the service named ``name``.

                :param name: The name of the service to stop.
                :param aspects: The aspects that control how the command is run.

                :returns: The status, stdout and stderr of the stop command.
            """
            command = "sudo systemctl stop {}".format(name)
            status, stdout, stderr = self._ssh_agent.run_cmd(command, aspects=aspects)
            return status, stdout, stderr

        def is_service_active(self, name: str, *, aspects: AspectsCmd = DEFAULT_CMD_ASPECTS) -> bool:
            """
                Checks whether the service named ``name`` is active.

                :param name: The name of the service to check.
                :param aspects: The aspects that control how the command is run.

                :returns: True if the service is active, otherwise False.
            """
            command = "systemctl is-active {}".format(name)
            status, _, _ = self._ssh_agent.run_cmd(command, aspects=aspects)
            active = status == 0
            return active

Passing Aspects From the Calling Code
-------------------------------------

The caller decides how much persistence each call needs, based on what the test is trying to do at that
point. The same API is used for every step, only the aspects change.

.. code-block:: python

    from ccai.interop.aspects import ActionPattern, AspectsCmd, LoggingPattern

    # Run the check until the service is active, for up to five minutes.
    UNTIL_ACTIVE = AspectsCmd(action_pattern=ActionPattern.DO_UNTIL_SUCCESS,
                              completion_timeout=300, completion_interval=5)

    # Run the check for as long as the service is active, for up to two minutes.
    WHILE_ACTIVE = AspectsCmd(action_pattern=ActionPattern.DO_WHILE_SUCCESS,
                              completion_timeout=120, completion_interval=2)

    def test_service_restart(service_agent: ServiceAgent):

        service_agent.stop_service("nginx")
        service_agent.is_service_active("nginx", aspects=WHILE_ACTIVE)

        service_agent.start_service("nginx")
        service_agent.is_service_active("nginx", aspects=UNTIL_ACTIVE)

        return

In the example above, ``is_service_active`` did not need ``wait_for_stop`` or ``wait_for_start`` variants
or timeout parameters of its own. The test expressed "wait until it stops" and "wait until it starts" by
passing different aspects. If the service does not reach the expected state in time, ``run_cmd`` raises a
:class:`TimeoutError` that includes the output of the last attempt.

The same APIs also have to serve tests that expect a call to fail. In the next example the test wants
exactly one attempt, and it does not want the refusal it is expecting to be logged as an error.
``SINGLE_CALL`` is already the default, but passing it explicitly states the intent of the test and
guards against an agent that was created with a retrying default.

.. code-block:: python

    # Make exactly one attempt and only log the result if the command succeeds.
    SINGLE_ATTEMPT = AspectsCmd(action_pattern=ActionPattern.SINGLE_CALL,
                                logging_pattern=LoggingPattern.SUCCESS_ONLY)

    def test_unprivileged_stop_is_refused(guest_service_agent: ServiceAgent):

        status, stdout, stderr = guest_service_agent.stop_service("nginx", aspects=SINGLE_ATTEMPT)

        if status == 0:
            errmsg = "An unprivileged user was able to stop the 'nginx' service."
            raise AssertionError(errmsg)

        return

With a `@retry` decorator on ``stop_service``, this test would sit through every retry before it could
check the result, and nothing in the test would show that the retries happened.

Passing aspects keeps the application layer in charge. An API only persists, retries or waits when the
caller asks it to through the aspects it is given, which is the contract the `Exception Handling`_ rules
refer to. Because aspects objects are frozen and passed through, a test can define its aspects once as
module level constants and reuse them across many APIs, and new behaviors can be added to the aspects
classes without changing the signature of any API. This pattern scales, and code needs to be consumable,
reliable and maintainable at scale.
