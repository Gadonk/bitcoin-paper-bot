# Bitcoin paper-bot v3: Unraid registry releases

Prepared for GitHub account Gadonk. The registry image does not exist until the first release is published and its package visibility is made public.

## How updates work

A stable GitHub release builds and tests the image, then publishes it to ghcr.io/gadonk/bitcoin-paper-bot:v3. Unraid checks that remote image and downloads a newer release when you choose Update. You do not upload source or rebuild on Unraid for future releases. No Community Applications listing is required. The bot connects to your existing Ollama; it does not install Ollama.

## One-time publication

Create the GitHub repository Gadonk/bitcoin-paper-bot using ONLY the files in this clean directory. Choose public source only with the owner's approval. GitHub Actions uses its own GITHUB_TOKEN; do not add a personal token to the source. Publish a stable release named v3.0.0. The Dockerfile runs the test suite before publication. After the first successful build, make the bitcoin-paper-bot container package Public in GitHub package settings (packages initially default to private). Verify an anonymous pull before switching Unraid. No image has been published by preparing this bundle.

## Existing v3 installation

After publication, stop v3 and back up its appdata directory. In Unraid Docker, edit the existing v3 container. Change only Repository from bitcoin-paper-bot-v3:local to ghcr.io/gadonk/bitcoin-paper-bot:v3 and Apply. Preserve the current host paths, port and environment values. Saved balance, trades and dashboard AI connection settings are in appdata and remain there. Keep v1/v2 on their own paths. A registry update does not replace your host config.json or reset the session.

## Fresh installation

After publication, copy this directory to Unraid and run bash install-unraid-registry.sh. Then Docker > Add Container > bitcoin-paper-bot-v3 > Apply. Open http://YOUR-UNRAID-IP:8090. Set your existing Ollama URL and installed model in the dashboard, enable AI and save. Use a LAN IP and port, or a Docker name on a shared custom network. AI is disabled initially. Standard local Ollama needs no API key. Start balance is 500 simulated DKK; no real orders are placed.

## Future releases

Change source, run checks, and publish the next stable v3.x.y GitHub release. Only published stable releases publish images; ordinary pushes and pull requests only run checks. In Unraid use Check for Updates, then Update. The moving v3 tag stays within major version 3. Each release also has a version tag, for example v3.0.0, for an explicit rollback; restore a matching appdata backup if a later release changes its data format. Release notes must explain any config or state migration.

## Validation and privacy

This directory includes source, sample configuration and tests only. It excludes live appdata, saved AI keys, trading exports and previous bundles. The public image contains the application source and sample configuration. Local Windows tests can run with python -m unittest discover -s tests -v. The Linux container build, including POSIX tests, must succeed in GitHub Actions before the image is available. The publisher targets linux/amd64 (ordinary Intel/AMD Unraid).

References: https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry and https://docs.unraid.net/community-applications/
