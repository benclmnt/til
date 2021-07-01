---
title: Sunfire as Git Server
tags: [git]
date: 2020-10-02
draft: false
---
# Setting up sunfire as a remote git server

## Background

CS2105 HW involves testing in a remote server (sunfire) and I want to use git.
So, I need to use it as a git server, and then I can sync changes using my usual git workflow.

## Problem

Encountered `command: git-upload-pack not found`, although `git` is installed

## Solution

Digging, the above problem is caused because `git-upload-pack` is not in the non-interactive `$PATH` used by git when ssh.

### Manual solution

```
git clone --upload-pack /path/to/git-upload-pack remote-server:~/path/to/remote/repo
git fetch --upload-pack /path/to/git-upload-pack remote-server:~/path/to/remote/repo
git pull --upload-pack /path/to/git-upload-pack remote-server:~/path/to/remote/repo
git push --receive-pack /path/to/git-receive-pack remote-server:~/path/to/remote/repo
```

Note: `/path/to/git-upload-pack` can be found by running `which git`.

### Automated solution

#### Change in server side

Add your `/path/to/git` to your non-interactive shell `$PATH`.
You can do this by adding it to `~/.bashrc` before the following line which is included by default in Ubuntu's `~/.bashrc`

```
# If not running interactively, don't do anything
[ -z "$PS1" ] && return
```

#### Changes per-repo

You add the following lines to your repo's `.git/config`

```
[remote "origin"]
    uploadpack = /path/to/git-upload-pack
    receivepack = /path/to/git-receive-pack
```

