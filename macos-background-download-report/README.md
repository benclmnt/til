# macos-background-download-reporter

./run.sh is an executable macOS-focused report script that uses the same signals from this investigation.

It currently shows:
- top active remote receivers grouped by process
- recent nsurlsessiond bundle IDs when the unified logs expose them
- softwareupdated progress and automatic-update status
- staged mobileassetd assets under /System/Library/AssetsV2/downloadDir
- recent mobileassetd clients from the unified log

Example usage:

```
sudo ./run.sh
sudo ./run.sh --minutes 60 --top 15
```
