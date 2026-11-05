# instance/

Data and settings for one specific rssFriends instance. The app code
never needs anything in here; every file is optional.

| File | Used by | Without it |
|---|---|---|
| `collections.json` | `app/db.py` seeds official collections on startup (path override: `RSSFRIENDS_SEED_FILE`) | No seed collections |
| `deploy.mk` | `make deploy` / `make deploy-check` (`DEPLOY_HOST`, `DEPLOY_PATH`, `DEPLOY_COMPOSE`, `HEALTHCHECK_URL`) | Pass them on the command line |

Everything here except this README is gitignored, so a public clone
never commits instance data by accident. A private instance repo that
wants to track its files adds them with `git add -f`.

`collections.json` is a JSON list of objects:

```json
[
  {
    "slug": "my-topic",
    "name": "My topic",
    "description": "What this collection is about.",
    "language": "en",
    "locale": "",
    "is_official": true,
    "curator_handle": null,
    "feeds": [
      ["https://example.com/feed.xml", "Example", "https://example.com/", "", "Why it's here."]
    ]
  }
]
```

Each feed is `[url, title, site_url, favicon_url, note]`.
