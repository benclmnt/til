# Passwordless ssh login

If I want to log in to a remote server without entering password everytime,
here's how to set it up.

```bash
# Create a unique ssh public/private key for this server with a comment of your server name. When prompted for password just fill in blank, because the purpose is to NOT enter any pswd.
ssh-keygen -t rsa -b 4096 -f ~/.ssh/<ENTER YOUR ID NAME> -C "<NAME OF SERVER>"

# Change permission of our key
chmod 600 ~/.ssh/<ENTER YOUR ID NAME>

# Copy key to remote server
ssh-copy-id remote_username@server_ip_address
# Or do this if the above command fails
# cat ~/.ssh/<ENTER YOUR ID NAME>.pub | ssh remote_username@server_ip_address "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Optionally create an alias to simplify login. In `.aliases` or any file where
you store aliases, add
```bash
alias remote='ssh -i ~/.ssh/<ENTER YOUR ID NAME> remote_username@server_ip_address'
```

or add the following lines to your local `~/.ssh/config` file
```bash
Host <server nickname>
    HostName <server name or ip address>
    User <your username>
    IdentityFile <path/to/you/id_name>
```

The reason we need to chmod 600 is that ssh do not like your private key to be seen by other people using the server.
