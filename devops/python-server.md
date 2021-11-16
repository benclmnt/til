---
title: Python setup in server
tags: [devops]
date: 2021-09-06
---

Problem: I have server access but I don't have root privileges.

## Pyenv

```bash
# Install pyenv
$ curl https://pyenv.run | bash

# Follow the instruction to modify ~/.bashrc

# Install the latest Python from source code
$ pyenv install 3.9.7

# Check installed Python versions
$ pyenv versions

# Switch Python version
$ pyenv global 3.9.7

# Check where Python is actually installed
$ pyenv prefix
/home/admin/.pyenv/versions/3.9.7
```

## Install pip

Download pip from an online repository and add the path to your bashrc.

```bash
wget https://bootstrap.pypa.io/get-pip.py
python3 get-pip.py --user
echo "PATH=$PATH:~/.local/bin" >> ~/.bashrc && source ~/.bashrc
```

## Install virtualenvwrapper

See more [here](https://virtualenvwrapper.readthedocs.io/en/latest/install.html)

`python3 -m pip install virtualenvwrapper`

## Resources

- https://docs.python-guide.org/dev/virtualenvs/
- https://snarky.ca/why-you-should-use-python-m-pip/
