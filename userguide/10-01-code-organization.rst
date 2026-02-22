.. _10-01-code-organziation-and-conventions:

===================================================
Automation Mojo - Code Organization and Conventions
===================================================
This document describes the organization of the code used by Automation Mojo for python repositories.

Root Directory
==============

The following is a list of folders that are can be found in the root folder of the repository.

- .cache - A temporary folder that can be used by scripts to store local user state that should not be checked in.
- .github - Folder which contains github scripts and workflow files
- .venv - A folder which is created when poetry is used to create the virtual environment for the project
- .vscode - A folder used by VSCode for the workspace settings, tasks and launch configurations
- deployment - A folder that contains scripts used to deploy the project or website
- development - A folder used to store developer scripts, script such as the 'setup-environment' script.
- repository-setup - A folder that contains the machine setup, 'rehome-repository' script and repository config files.
- specs - The root folder for code specification files such as design.md, requirements.md, tasks.md and others.
- source - The folder that contains the project source code
- workspaces - The workspaces folder stores vscode workspace template files '*.template' and '*.code-workspace' files.  The '*.template' are checked in and contain macros that can be used by the 'repository-setup/rehome-repository' script to create a '*.code-workspace' file that contains paths that have been re-homed based on where the repository was checked out to on the local users hard drive.

There are also some files located in the root of the repository folder.

- .env - A local file that ".env" file that is created by the 'repository-setup/rehome-repository' script.
- .gitignore - File indicating files and folders that git should ignore
- LICENSE.txt - A file that specifies the licensing terms of the source code contained in the repository.
- Makefile - A Makefile that contains targets for building and publishing the packages in the repository.
- poetry.lock - The poetrsy lock file associated with the python package for this folder.
- poetry.toml - A poetry project settings file.
- pyproject.toml - A file that describes the package contained in the folder and its dependencies
- README.md - A file that is the readme associated with the project.


'deployment' Folder
===================
The deployment folder, if present, contains scripts for deploying a website or services.  The following is a list of the
files:


'development' Folder
====================
The 'development' folder contains scripts and configuration files used by developers to initialize the machine and
repository configuration.  The following is a list of the files:


'repository-setup' Folder
=========================
The 'development' folder contains scripts and configuration files used by developers to initialize the machine and
repository configuration.  The following is a list of the files:


'source' Folder
===============
The 'source' folder contains sub-folders that are organized by the role that the code plays in the project.  The following
is a list of the folders commonly found directly under the 'source' folder:

- examples
- packages
- site
- testroots
- tools

The following sections describe the sub-folders, their usage, and contents.


'source/examples' Folder
------------------------
The *examples* folder contains scripts that provide examples of how to use the code from the project and that can also
be used to debug parts of the projects source code.


'source/docker' Folder
----------------------
The *docker* folder contains docker container creation files.


'source/jenkins' Folder
-----------------------
The *jenkins* folder contains groovy script files that are used to extend the Jenkins continuous integration system and
job files that are used to enable the use of configuration as code to create jenkins jobs.


'source/packages' Folder
------------------------
The *packages* folder contains the python code that makes up any python packages created by the project.  The packages
folder contains a folder 'automojo' which is the root namespace package for all Automation Mojo projects.  The next folder
under the 'automojo' folder is generally named after the package itself.  For example, for the 'automojo-errors'
distribution package.  There will be a folder 'source/packages/automojo/errors'.  The 'source/packages/automojo' is a
namespace package so it does not contain an '__init__.py' file.   The next folder down, in this case the
'source/packages/automojo/errors' folder, is the first folder that would contain a '__init__.py' file.

When you have a root namespace package, such as the 'automojo' package.  Then when you install different python
distribution packages such as 'automojo-errors' and 'automojo-foundation'.  The packages install into the common
namespace alongside each other in the virtual environment like:

    '.env/lib/<python-version>/site-packages/automojo/errors' and '.env/lib/<python-version>/site-packages/automojo/foundation'


'source/site' Folder
--------------------
The *site* folder contains a hierarchy of files and folders that are used to create a flask web application including both
the graphical interface and REST API elements of a website or microservice.


'source/site/\<namespace\>' Folder
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The *<namespace>* folder provides the root namespace for python imports related to the website server side code.  The
*PYTHONPATH* variable is set to point to the *source/site* folder and imports are made relative to the *site* folder.
Generally the name used for the namespace folder is the root namespace for the company and project.  For example, the
root namespace for a hello world service written by the company *Automation Mojo* would be *source/site/automojo/hello*
and the code would be imported like:

    .. code-block:: python

        from automojo.hello.restapi.v1.HelloMessage import HelloMessage

        from automojo.hello.webinterface.blueprints.home import HomeBlueprint


'source/site/<namespace>/restapis' Folder
+++++++++++++++++++++++++++++++++++++++++++
The *restapis* folder contains the implementation of REST API endpoints for the web application or microservice. These
endpoints define the server-side logic for handling HTTP requests and responses.

'source/site/<namespace>/webinterface' Folder
+++++++++++++++++++++++++++++++++++++++++++
The *webinterface* folder contains the server-side code for the web application's graphical interface. This includes
templates, views, and other components that define the structure and behavior of the webinterface.

'source/site/<namespace>/webinterface/blueprints' Folder
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
The *blueprints* folder organizes the Flask application into modular components. Each blueprint represents a distinct
feature or section of the web application, making the codebase easier to maintain and scale.

'source/site/<namespace>/webinterface/blueprints/<blueprint-name>' Folder
########################################################################
The *<blueprint-name>* folder contains the implementation details for a specific blueprint. This includes route definitions, views, and any associated templates or static files for that feature.

'source/site/<namespace>/webinterface/static' Folder
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
The *static* folder contains static assets for the web application, such as CSS, JavaScript, images, and other files
that do not change dynamically.

'source/testroots' Folder
-------------------------
The *testroots* folder contains test configurations, test dependency originations and test cases for projects that use 'testbase' (single host testing) or 'testplus' (distributed testing) as their
test framework.  For repositories that use 'testbase', the tests are organizded under a folder using a naming template as follows:

    'source/testroots/<org-namespace>/tests/<package-namespace>/<module-path>/test_<functionality>.py'

For repositories that use 'testplus', the tests are organized based in the automation configuration they run under using a naming template as follows:

    'source/testroots/<org-namespace>/tests/<automation-configuration>/<package-namespace>/<module-path>/<testroot-name>/<functionality>.py'

The '<automation-configuration>' is that name of the automation configuration that the test is running under.  Automation configuraitons provide specific
test resources and dependencies that are used by the test.  The '<org-namespace>' for all automation mojo repositories is 'automojo'.  The '<package-namespace>'
for a package is generally the second part of the package name.  For example, the 'automojo-interop' would use a package name like 'automojo.interop' for the
package namespace and 'automojo.tests.interop' for the prefix for all of the packages tests. 

'source/tests' Folder
----------------------
The *tests* folder contains the test cases for the project.  The test cases are organized into folders that are named
after the package that they are testing. 


'source/tools' Folder
---------------------
The *tools* folder contains utility scripts and tools that assist in the development, debugging, or maintenance of the
project. These tools may include custom scripts for automation, data processing, or other tasks.

