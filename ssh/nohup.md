# Nohup

To use `nohup`, you also need to redirect both stdin and stderr to a file or `/dev/null`.

Example: `nohup python3 -u run_clients.py --db $db -n $i -i $workloadpath >logs/$db/$host.log 2>&1 &`

- https://unix.stackexchange.com/questions/104487/what-happens-to-a-continuing-operation-if-we-do-ssh-and-then-disconnect
- https://unix.stackexchange.com/questions/133951/how-to-terminate-remotely-called-tail-f-when-connection-is-closed
- https://unix.stackexchange.com/questions/346549/why-doesnt-ssh-t-wait-for-background-processes
