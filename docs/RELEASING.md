# Releasing

A release is a version tag. Pushing a `vX.Y.Z` tag runs the [release workflow](../.github/workflows/release.yml), which does three things:

1. It checks that the tag matches the version in every version file (`scripts/release_check.py`). If one file differs, nothing is published.
2. It publishes the npm package `agents-over-telegram`, with provenance. Pi installs it with `pi install npm:agents-over-telegram`.
3. It creates a GitHub release for the tag, with notes that GitHub generates from the merged pull requests.

Claude Code, Codex and Antigravity install from this repository, so for them the tag and the `main` branch are the release.

## Make a release

1. Raise the version in every version file and merge the change into `main`. `python3 scripts/release_check.py vX.Y.Z` lists any file you missed.
2. Tag the merge commit and push the tag:

   ```sh
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

## One-time setup

The repository owner does these steps once.

### npm

npm trusted publishing lets the workflow publish without a stored token. npm can link a trusted publisher only to a package that already exists, so the first version is published by hand:

1. Sign in to npm and publish the current version from a clean checkout of the tag: `npm publish`. The package `files` list selects what is published; check it first with `npm pack --dry-run`.
2. On npmjs.com, open the package settings and add a trusted publisher of type GitHub Actions:
   - Organization or user: `fourlastor`
   - Repository: `agents-over-telegram`
   - Workflow filename: `release.yml`
   - Environment name: `npm`
3. In the same settings, set publishing access to require two-factor authentication and disallow tokens. The workflow does not need a token.

### Protect the release tags

Anyone who can push a `v*` tag can publish. Limit that in the repository settings:

1. Add a tag ruleset for `v*` that allows only the maintainers to create, update or delete these tags.
2. The workflow publishes from the GitHub environment `npm`, which GitHub creates on the first run. In its settings, limit deployments to tags that match `v*`. You can also add a required reviewer.

### Official plugin directories

Anthropic's plugin directory, Google's Antigravity partner marketplace and OpenAI's plugin directory review each listing. The first submission is a manual step from the owner's account. After that, updates follow the repository or the tag where the directory supports it.
