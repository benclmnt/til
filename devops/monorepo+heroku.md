# Deploying monorepo + heroku

Related repo: [CS2102 Project Repo](https://github.com/benclmnt/CS2102_2021_S1_Team28/blob/master/heroku-deploy.py)

Based on:
1. [Heroku-Node-Pg](https://www.taniarascia.com/node-express-postgresql-heroku/)
2. [Monorepo + Heroku](https://chunkofcode.net/deploying-a-monorepo-with-backend-and-frontend-directory-to-heroku-using-git-subtree/)

## What I've tried (but don't work)

- Buildpacks
- Manually building by utilizing `postinstall` script which will run after Heroku finish installing dependencies. Heroku will complain `cannot find dependency`.
- Building the application on process run. Not feasible: Heroku will terminate your process if it doesn't bind to a port within 60 secs.
- Checking in the build files to git (but still in its own packages). Heroku will complain `cannot find dependency` since it only installs the root's dependency.

## Solution

My solution can be seen [here](https://github.com/benclmnt/CS2102_2021_S1_Team28/blob/master/heroku-deploy.py). I have the `heroku` remote set to 

```bash
$ git remotes
heroku  git@heroku.com:cs2102-petsos.git (fetch)
heroku  git@heroku.com:cs2102-petsos.git (push)
```

Summary:

1. Build the project locally
2. Copy the resulting files into a separate folder, in my case it is called `web`
3. Utilize `git subtree` to only push the `web` folder to Heroku

## What heroku expects

- Heroku expects a Procfile for a process called `web`, else it will run the `start` script specified in `package.json`.
- Heroku will install root dependencies (and dev dependencies), then builds, then prune dev dependencies. There are some config vars that you can set to disable pruning dev dependencies.
- If you use the Heroku Postgres Addon, a `DATABASE_URL` environment var will be inserted that you can access from your app.

## Caveats

- Heroku expects the project in the repo's root folder. This means:
  - Heroku will only install dependencies listed in root's `package.json` file
  - Will run the `build` script of root's `package.json` file

- Customize how Heroku builds your project by specifying a `heroku-postbuild` script in your `package.json`. Else, it will run your `build` script by default.



