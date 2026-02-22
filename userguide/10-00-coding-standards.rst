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

The 'AutomationKit' is a test framework and library so a majority of the function, class and method
implementations should behave like a library component and release control over the resolution of
exceptional conditions to the 'Application' level of code. In the case of a test run, the code that
would be considered to be at the 'Application' level is the code that is controlling the flow of execution.

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

3. 'AutomationKit' exceptions will display the code from which they were raised. An API or library SHOULD catch
   and reclassify an exception if doing so will improve the code context view related to the nature of the problem
   that caused the exceptional condition. (raise exceptions from the code you want to show in the test failure reports)

4. An API or library SHOULD catch an exception and add "EXTRA" information to the exception if doing so
   will provide contextual information about unique circumstances that cause a specific exception

5. 'SemanticError' should NEVER be handled. There are raised when an API is being used improperly. This means there
   is a definitive test code failure. They should ALWAYS be allowed to bubble so the improper usage can be fixed.

6. In all other cases, exceptions should be allowed to bubble

Special Exceptions
------------------

The 'AutomationKit' utilizes very specific exception in order to classify or categorize exceptional conditions
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
of a target under test. The 'AutomationKit' provides the 'AssertionError' for this purpose. Raising a normal
'AssertionError' or the 'AutomationKit' derivative class 'AssertionError' will result in an issue being classified
as a failure of condition or state associated with the target of the test scenario.

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
------------

Finally, `return` statements are important to improve the legibility of code. Because python code
uses indentation to determine scope, the repeated indentation of successive code blocks can present
issues with the readability of code. This can particularly be a problem with longer functions.

For consistency and to help resolve all of these issues, It is preferred to use returns on all functions
and methods. Any function or method that is not a generator, since generators don't have returns.

InterOp and Persistence Decorators
==================================

Distributed automation scenarios engage in an awful lot of interop activity. When your engaging in
any type of interop activity, there can be problems with reliability. One of the major aspects of
distributed testing is to place reliability expectations on the performance of interop APIS under
both success and failure conditions. Let me highlight that, test framework APIs **must** be usable by
test code for both **success** and **failure** conditions. They must allow for the caller to pass
**valid** and **invalid** data so the behavior of the remote endpoint can be tested under different
conditions.

An experienced Test Framework Architect is aware of this important aspect of test framework APIs that is
much different from maybe a production API and will design the APIs accordingly.

One pattern that you might see in a production environment for APIs is the use of a `@retry` decorator
on an API that might not have 100% reliability.

.. code-block:: python

  @retry(attempts=3)
  def run_command(command):
      status, stdout, stderr = self.ssh_agent.run_cmd(command)
      return
  
This might work fine in a production environment where calling the API successfully is the main goal,
but for test code, our main goal for interop APIs is not just to make successful calls. We also have
several competing goals:

* Check expected behavior (for success and failure)
* Capture detailed data in failure contexts
* Let tests and higher level code have control of behaviors and expectations

The use of retry decorations negatively impacts these goals. First of all, a `@retry` decoration always
assumes a call should succeed. Good test framework code **does not assume** or make decisions for the
code that is controlling the flow of a test. Flow control and error handling should be the responsibility
of the higher level test code. Good test framework code lets the higher level code **make expectations**
on the behavior of the code under test in varying conditions. Good test framework code also allows
the higher level code to make decisions about behavior under success and failure conditions so the
higher level code can decide what to do based on the current context of the test.

A better approach to design inter-operability APIs used by the **Automation Kit**
is to allow the tests to pass parameters to the API to allow higher level code to change the behavior of
the APIs.

The pattern that should be used is to tack on behavior modification parameters to the end of the
inter-op APIs. Another important aspect of designing APIs and behavior parameters is to be consistent.
An example of a behavior parameter might be that on every API that modifies the remote state shared by
devices should have a `sync` parameter to indicate the API should wait for a `sync` of shared state. 

.. code-block:: python

  def add_member(name: str, sync: bool=True) -> int:
      return id
  
  def delete_member(id: int, sync: bool=True):
      return

  def get_members() -> List[str]:
      return

  def rename_member(id: int, new_name: str, old_name: str, sync: bool=True):
      return

Note on the above API(s), the APIs that `add`, `delete` and `modify` data have a sync parameter and
the APIs that do not modify state do not offer a sync behavior.

This sort of adding parameters can be a problem when you have lots of different ways you want
to offer control over a set of interop API(s). You can quickly have a longer list of parameters
that are needed to control the behavior of the lower level code. If you have 500 interop APIs
in your test framework, this can lead to maintenance issues when adding new parameters. The
**Automation Kit** deals with this nicely by having one Object parameter, that is used to contain
the behavior modification parameters. Here is an example of a way to handle this that will scale
nicely.

.. code-block:: python

  class Aspects:
      """
          Aspects are utilized with the interop APIs and agents such as the :class:`SSHAgent` class in order
          to modify the behavior of APIs with respect to retry parameter such as timeout, interval, looping patterns
          logging, etc.  The aspects object provides a way to package this common criteria into a single parameter
          or constant you can  pass to multiple APIs
      """

      def __init__(self, action_pattern: ActionPattern = ActionPattern.SINGULAR, completion_timeout: float = DEFAULT_COMPLETION_TIMEOUT, completion_interval: float = DEFAULT_COMPLETION_INTERVAL,
                        inactivity_timeout: float = DEFAULT_INACTIVITY_TIMEOUT, inactivity_interval: float = DEFAULT_INACTIVITY_INTERVAL, monitor_delay: float = DEFAULT_MONITOR_DELAY,
                        logging_pattern: LoggingPattern = DEFAULT_LOGGING_PATTERN, logger: Optional["Logger"]=None):
          """
              Creates an :class:`Aspects` package.

              :param action_pattern: The :class:`ActionPattern` that the API should exhibit such as SINGULAR, DO_UNTIL_SUCCESS, DO_WHILE_SUCCESS
              :param completion_timeout: The time in seconds as a float that is the max time before timeout for the activity to complete.
              :param completion_interval: The time in seconds as a float that is waited before reattempting an activity.
              :param inactivity_timeout: The time in seconds as a float that is the max time before timeout that is waited before a :class:`TimeoutError`
                                        is raised due to inactivity.
              :param inactivity_interval: The time in seconds as a float that is waited before reattempting an activity.
          """
          self.action_pattern = action_pattern
          self.completion_timeout = completion_timeout
          self.completion_interval = completion_interval
          self.inactivity_timeout = inactivity_timeout
          self.inactivity_interval = inactivity_interval
          self.monitor_delay = monitor_delay
          self.logging_pattern = logging_pattern

          if logger is None:
              self.logger = getAutomatonKitLogger()
          else:
              self.logger = logger

          return

This pattern scales well and very very very important aspects of how we write code is that it
needs to be consumable, reliable and must be maintainable at scale.


