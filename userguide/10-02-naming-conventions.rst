.. _10-02-naming-conventions:

==================
Naming Conventions
==================


Constants
=========
Constants should be named using uppercase letters with underscores separating words. This makes them easily distinguishable from variables and functions.

.. code-block:: python

    # Correct
    MAX_CONNECTIONS = 100
    TIMEOUT_SECONDS = 30
    PI = 3.14159

    # Incorrect
    maxConnections = 100
    timeoutSeconds = 30
    pi = 3.14159

When defining constants, use descriptive names that clearly indicate their purpose. This improves code readability and maintainability.  Constants should be
defined in a separate module or file if they are used across multiple modules.  This helps in organizing the code and makes it easier to manage changes to constants.

Constants should be prefixed with a symbolic name which that describes the type of constant they represent. For example, if a constant represents a configuration value,
it could be prefixed with `CONFIG_`.


Method Names
============


Conversion Methods
------------------
Methods that convert an object to another binary data type such as a dictionary but do not serialize it should be named `as_<type>`.  This makes it clear that the method
is converting the object to a different type without effecting the usability of the data.

If an object is complex and it is desirable to have a brief representation of the object, the method can take an optional boolean parameter called `brief`. If `brief` is
set to `True`, the method should return a single level or simplified version of the object.

.. code-block:: python

    # Correct
    def as_dict(self, brief: bool=False) -> dict:
        rtnval = {'key': self._key}
        return rtnval


Encoding Methods
----------------
Methods that serialize an object to a string of a specific format should be named `to_<format>`. This makes it easy to locate the serialization methods of objects.

.. code-block:: python

    # Correct
    def to_json(self) -> str:
        rtnval = json.dumps(self.as_dict())
        return rtnval

    def to_xml(self) -> str:
        # Implementation for XML serialization
        rtnval = "<object><key>{}</key></object>".format(self._key)
        return rtnval

    def to_yaml(self) -> str:
        # Implementation for YAML serialization
        rtnval = yaml.dump(self.as_dict())
        return rtnval
