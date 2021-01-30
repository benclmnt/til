# Git tricks

## Force pull

```
git fetch origin master
git reset --hard origin/master
```

## Add deleted files to index

- `git add .` tracks file deletion (but adds other files as well)
- For the index to track single file deletion, `git add $(git ls-files --deleted)` can do the job.
- If you want to also handle space in file name, you can do
  ```
  SAVEIFS=$IFS 
  IFS=$(echo -en "\n\b") # change IFS to \n\b to handle space in filename :(
  git add `git ls-files --deleted`
  IFS=$SAVEIFS
  ```
  
## Edit last commit 

`git commit --amend --no-edit`
