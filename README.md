# Two Hard Things F-Droid repo

An [F-Droid](https://f-droid.org/) repository for the apps by Two Hard Things,
published at **https://twohardthings.github.io/fdroid/**.

Each app is built and signed in its own GitHub repository. This repository
holds no app code and builds nothing: it republishes the apps' signed release
APKs, after checking them, as an F-Droid repo on GitHub Pages.

| App | Source |
| --- | --- |
| Time Cop Fork | [twohardthings/timecopfork](https://github.com/twohardthings/timecopfork) |

## Add the repo to F-Droid

Open https://twohardthings.github.io/fdroid/ on your phone and tap **Add to
F-Droid**, or scan its QR code. Both include the repo's fingerprint, so your
F-Droid client refuses the repo if it isn't signed by our key.

To add it by hand, use the address and fingerprint shown on that page. The
fingerprint is also in [fingerprint.txt](fingerprint.txt).

## How it stays trustworthy

- **Apps:** for every APK, [scripts/fetch_releases.py](scripts/fetch_releases.py)
  checks that:
  - the download matches the SHA-256 digest GitHub reports for the release asset;
  - the package name is the one listed in [apps.yml](apps.yml);
  - the APK is signed by exactly the certificate pinned there.

  fdroidserver checks the signer a second time (`AllowedAPKSigningKeys` in
  `metadata/<app>.yml`). Any failed check stops the run, and the published repo
  stays as it was. Because the APKs are the apps' own signed releases, users can
  switch between this repo, GitHub Releases and Obtainium and still get updates.
- **The repo itself:** the index is signed with this repo's own key. The key is
  kept only in the `FDROID_KEYSTORE` secret of the `fdroid-signing` environment,
  which only the `main` branch can use. Each run checks that the key matches
  [fingerprint.txt](fingerprint.txt), and the key is deleted when the job ends.
- **Build tools:** fdroidserver and all its dependencies are installed by hash
  ([requirements.txt](requirements.txt)), and the GitHub Actions are pinned to
  commit SHAs. The workflow never runs for pull requests.

## Publishing

[The publish workflow](.github/workflows/publish.yml) rebuilds the whole repo
from the latest `keep_releases` releases of each app and deploys it to Pages.
It runs:
- on every push to `main`;
- daily at 04:17 UTC;
- when an app's release workflow triggers it (see "Trigger from an app's
  releases" below).

## Setup (once)

1. **Create the repo signing key** on your own computer, not in CI, and keep a
   backup offline. If it's lost, every user has to remove and re-add the repo.

   ```bash
   keytool -genkeypair -storetype PKCS12 -keystore fdroid-repo.p12 -alias repo \
     -keyalg RSA -keysize 4096 -sigalg SHA512withRSA -validity 10000 \
     -dname "CN=Two Hard Things F-Droid repo"
   ```

   Then print its fingerprint and commit it as `fingerprint.txt`:

   ```bash
   keytool -exportcert -keystore fdroid-repo.p12 -alias repo | sha256sum | cut -c1-64
   ```

   (On macOS, use `shasum -a 256` instead of `sha256sum`.)

2. **Add the secrets.** Under Settings > Environments, create `fdroid-signing`.
   Set "Deployment branches and tags" to selected branches, `main` only, and
   add these environment secrets:
   - `FDROID_KEYSTORE`: the keystore in base64 (`base64 -w0 fdroid-repo.p12` on
     Linux, `base64 -i fdroid-repo.p12` on macOS).
   - `FDROID_KEYSTORE_PASS`: the keystore password.

3. **Turn on Pages.** Under Settings > Pages, set Source to "GitHub Actions".

4. **Run the workflow** (Actions > Publish F-Droid repo > Run workflow).

## Add an app

1. Add an entry to [apps.yml](apps.yml) with the app's package name, GitHub
   repository, signing certificate SHA-256, release asset pattern and (optional)
   fastlane metadata path.
2. Add `metadata/<package name>.yml` with the license, links and the same
   certificate in `AllowedAPKSigningKeys`. See
   [Time Cop Fork's](metadata/io.github.twohardthings.timecopfork.yml).
3. Optionally, trigger the repo from the app's releases (below).

## Trigger from an app's releases

To publish new releases right away instead of with the next daily run:

1. Create a fine-grained personal access token that is limited to the
   `twohardthings/fdroid` repository and has only **Actions: Read and write**.
   That permission lets it start this workflow, but not change any files.
2. Add it to the app's repository as the secret `FDROID_DISPATCH_TOKEN`.
3. Add a step to the end of the app's release job:

   ```yaml
   - name: Update the F-Droid repo
     if: env.IS_RELEASE == 'true'
     env:
       TOKEN: ${{ secrets.FDROID_DISPATCH_TOKEN }}
     run: |
       [ -n "$TOKEN" ] || { echo "No FDROID_DISPATCH_TOKEN; the daily run will pick it up."; exit 0; }
       curl -fsS -X POST -H "Authorization: Bearer $TOKEN" \
         -H "Accept: application/vnd.github+json" \
         https://api.github.com/repos/twohardthings/fdroid/actions/workflows/publish.yml/dispatches \
         -d '{"ref":"main"}' || echo "::warning::Couldn't trigger the F-Droid repo; the daily run will pick it up."
   ```

## Updating fdroidserver

Change the version in `requirements.in`, then regenerate the pins:

```bash
pip install pip-tools
pip-compile --generate-hashes --strip-extras -o requirements.txt requirements.in
```
