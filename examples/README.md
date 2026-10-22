# Run in Docker

`docker-compose.example.yml` is a quick-start `tax` service for a stack that already
runs Paperless-ngx. It defines only the tax service and an external network.

It needs the first release after v0.1.0 that supports `PAPERLESS_TOKEN`. The 0.1.0
image reads the token only from `--token-file`.

## Setup

1. Copy the `tax` service and the `networks` block into your stack, or use
   `docker compose -f` with both files.
2. Replace `<version>` with a released image version.
3. Replace `<your-paperless-network-name>` with the network Paperless runs on.
4. Create the dotenv file outside the repository and outside the stack directory,
   then restrict it:

   ```sh
   cat > /path/outside/stack/paperless-tax-export.env <<'ENV'
   PAPERLESS_TOKEN=<view-only-api-token>
   PAPERLESS_BASE_URL=http://<paperless-service-name>:8000
   ENV
   chmod 600 /path/outside/stack/paperless-tax-export.env
   ```

   The service name and port are the ones your own stack uses. Use the token of a
   view-only Paperless user. Never commit this file.
5. Set the `env_file` and output bind-mount paths in the compose file. The image
   runs as the non-root user `app`; under rootless Docker, check that the output
   directory is writable by that user.

## Commands

The `tax` profile keeps the service out of `docker compose up`.

```sh
docker compose --profile tax run --rm tax --version
docker compose --profile tax run --rm tax audit
docker compose --profile tax run --rm tax run --dry-run
docker compose --profile tax run --rm tax run --expected-count N
```

`N` is the number of documents the Paperless admin view shows for the tag with the
financial-year `created` filter applied. `--dry-run` runs every guard and writes
nothing.

## Output

Files go to the mounted directory (`/app/output` in the container):

- `review.csv`: documents that need a human decision, with reason codes.
- `deductions.csv`: documents that pass every check.
- `investments.csv`: index of the configured investing correspondent's statements.
- `bills.csv`: the utilities and bills of the year (documents of type `bill`), with a kind from a tag.
- `estimate.csv`: an estimate of total deductions in AUD (not tax advice), with illustrative
  work-from-home scenarios from the bills.
- `manifest.json`: parameters, versions, counts and file hashes.
- `audit.json`: written by `audit`.
