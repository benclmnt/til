# Blogspot Migration script

Script to migrate your old blogspot migration in case Google shuts down blogspot.com

## How to run

1. Get sidebar to turn on all by pasting these script in the browser console.
```js
document.querySelectorAll('.zippy').forEach(x => x.click());
# sleep 10s
let posts = [];
document.querySelectorAll('.posts').forEach(x => x.querySelectorAll('li a').forEach(y => posts.push(y.href)));
```
2. Paste this into `expected.json` file
3. Run `uv run compare_urls.py`
4. Copy the theme from one of the pages into `styles/theme.css`
5. Copy `archive-widget.js` into `scripts/` and update the list of urls from `expected.json`.
6. Run `uv run rewrite_posts.py`.