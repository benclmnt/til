# Passwordless ssh

We are going to connect from our computer (A) to a remote server (C) via a bridge server (B).

Assuming you are able to ssh from A to B and from B to C without any password,

First way : `ssh -t B ssh C`

Second way:
1. copy B's private key to A. The private key should be the one used to passwordlessly login to C.
2. Add the following lines to A's `~/.ssh/config`
```bash
Host <server C nickname>
    HostName <server name or ip address>
    User <your username>
    IdentityFile <path/to/you/id_file_copied_from_B>
    ForwardAgent yes
    ProxyJump B
```
3. You can then just `ssh <server C nickname>`