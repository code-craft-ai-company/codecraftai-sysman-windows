# Personality
AI Agents working in this repository should refer to themselves as an agent named 'AutomationX'.

# Rules
- Look in the '<repository>/specs' folder for specification files that help to steer development work
- Look in the '<repository>/userguide' folder for files containing standards and conventions
- Look in the '<repository>/prompts' folder for pre-build prompts
- Files inside the .venv folder should not be modified, they are modified by the setup scripts

# Project Overview

# Testing Conventions
Use this section to describe the test framework that should be used for testing code in this repository.  You can instruct the Agent on how to write tests for this repository by using the
following helper paragraphs:

This repository utilizes the 'unittest' test framework.  Detailed information about how to
utilize the 'unittest' test framework with this code base are located in the [Testing Frameworks - UnitTest](userguide/10-04-01-testing-frameworks-unittest.rst)

This repository utilizes the 'pytest' test framework.  Detailed information about how to
utilize the 'pytest' test framework with this code base are located in the [Testing Frameworks - PyTest](userguide/10-04-01-testing-frameworks-pytest.rst)

This repository utilizes the 'testbase' test framework.  Detailed information about how to
utilize the 'testbase' test framework with this code base are located in the [Testing Frameworks - TestBase](userguide/10-04-01-testing-frameworks-testbase.rst)

# Code Naming Conventions
The code naming conventions of this repository follows the [Naming Conventions](userguide/10-02-naming-conventions.rst) document.


# Code Structure
The code structure of this repository follows the [Code Organization](userguide/10-01-code-organization.rst) document

Remember to make the test hierarchy match the source hierarchy when adding test files.  For example:

File to test: 'source/packages/automojo/interop/protocols/dns/dnsquestion.py'
Test file location: 'source/testroots/automojo/interop/protocols/dns/test_dnsquestion.py'

# Coding Standards
The code in this repository is meant to be utilized by testing tools and test frameworks.  Therefore, the code must prioritize:

*  debuggability over performance
*  readability over conciseness

For all the code in this repository, follow the coding standards specified at [Coding Standards](userguide/10-00-coding-standards.rst)


# Environment Setup

## Codex Container Setup
When work is being done on this repository using the Codex web environment.  Use the script 'development/codex-setup' as the setup script for Codex containers.

## Development Environments
When checking out the code for this repository in order to test changes, agents must:

* run the 'repository-setup/rehome-repository' script.  It creates a needed '.env' file.
* run the 'development/setup-envrionment' script in order to create the virtual environment.

# Project Specific Rules


# Pull Request Guidelines


# Packaging
Currently, this package is utilized using a reference to the github repository.  It is currently not published.
