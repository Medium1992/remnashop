# Integration checks

These need a live Postgres, so they are not part of the unit suite. pytest does
not collect them: the filenames deliberately do not start with `test_`.

## check_migration_path.py

Proves, against a real database rather than a fake, the two things the identity
scheme rests on:

* the backfill selects exactly the rows an older RemnaShop wrote, and never one
  this build wrote -- which depends on how Postgres orders the `uuid` column;
* the SQL a future migration will run decodes every stored key back to the
  panel's numeric id, and leaves unresolved rows untouched.

Run it against a database that has had the migrations applied:

```bash
docker run --rm --network host \
  -e DATABASE_HOST=127.0.0.1 -e DATABASE_PORT=5432 \
  -e DATABASE_NAME=remnashop -e DATABASE_USER=postgres \
  -e DATABASE_PASSWORD=... \
  -v "$PWD/src:/opt/remnashop/src:ro" \
  -v "$PWD/tests:/opt/remnashop/tests:ro" \
  remnashop-test:local python tests/integration/check_migration_path.py
```

It deletes and rewrites the `users` and `subscriptions` tables, so point it at a
scratch database, never at production.
