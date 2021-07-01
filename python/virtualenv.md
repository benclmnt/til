---
title: Virtualenv
tags: [python]
date: 2020-08-18
draft: false
---
# Virtual Env

Using virtualenv is beneficial to keep the main python free from cluttered by
one-off libraries. Here's how to set up. For *NIX, change `python` to `python3`

```bash
python --version # check your python version
python -m pip install --upgrade pip # upgrade python package manager version

# create virtual environment
python -m venv /path/to/new/virtual/environment

# activate virtual environment
venv\Scripts\activate # for windows
# source ./bin/activate for *NIX

# Install package inside virtualenv
pip3 install <package-name>

# To run something using python inside virtual env
python <python-file> # the python command will refer to python inside the virtualenv folder. No need to use python3.

# When you're done, deactivate virtual environment
deactivate
```

You can delete all scripts installed in virtualenv by removing the `<venv folder>`

## Running IDLE from virtual env

To open idle from virtualenv, do `python -m idlelib.idle`