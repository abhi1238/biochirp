# `frontend/_dev/` — developer-only pages

Files in this directory are **internal debug tools**. They live under
`frontend/` so that the path-mapping the production nginx config uses for
the rest of the chat assets still resolves their relative imports, but the
leading underscore (`_dev`) is the convention for "do not expose publicly".

## Production deploy rule

The nginx vhost serving `frontend/` MUST deny requests for `/_dev/*`:

```nginx
location ^~ /_dev/ {
    return 404;
}
```

The snippet above is the whole rule — there is no separate drop-in file shipped in this repo.

## What's in here

_Currently empty._ The former `multi_db_test.html` debug console was removed
on 2026-06-18 together with the multi-DB / `bio_chat` backend (port 8030) it
drove — that aggregate path was decommissioned, so the console had nothing to
talk to. This directory and its production deny-rule are kept for future
dev-only pages.

## Adding a new dev page

1. Drop the file here.
2. Make sure it imports from `../assets/` if it shares chat assets.
3. Confirm the `location ^~ /_dev/ { return 404; }` rule above is applied on the deploy host.
