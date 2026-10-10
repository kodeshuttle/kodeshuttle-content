# kodeshuttle-content

Source repository for approved KodeShuttle MDX content and its offline validation and metadata-sync tooling. GitHub Actions can validate pull requests; the protected-main sync workflow keeps database writes disabled unless its explicit write gate is enabled.

## Content layout

- Articles use `articles/<category>/<slug>/index.mdx`; troubleshooting content uses `troubleshooting/<category>/<slug>/index.mdx`.
- Keep referenced assets beside their MDX file.
- Required frontmatter is validated by `scripts/content_sync.py`; publication state and approved revisions remain database-controlled.

## Local validation

Install the pinned validation dependencies, then run:

```sh
python3 -m pip install -r requirements-validation.txt
python3 -m unittest discover -s tests -v
python3 scripts/content_sync.py validate --commit "$(git rev-parse HEAD)"
```

Validation does not write to the database. Do not put credentials, private keys, or local environment files in this repository. The sync workflow uses a protected GitHub environment and requires an explicit write-enable setting; keep writes disabled during review and preview.
