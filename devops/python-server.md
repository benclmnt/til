---
title: Python setup in server
tags: [devops]
date: 2021-09-06
---

Problem: I have server access but I don't have root privileges.

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