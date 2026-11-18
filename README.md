# Chinese publisher collector

Mojito's nightly local refresh dispatches these workflows only for failed local routes and explicit browser-dependent recipes. Manual maintainer dispatch remains available. Text collection uses the registered publisher limits; browser collection reports its bounded listing coverage and any partial capture. Raw pages remain on the disposable VM. Public logs contain counts and status, and document bodies stay out of the public repository and Actions artifacts.

Choose **Send validated text to private intake** when dispatching the workflow to test the storage handoff. The `private-intake` environment holds the repository-scoped `INTAKE_UPLOAD_TOKEN`. Configure the environment to accept the default branch only. The destination must be an initialized private `mojito-chinese-intake` repository. The write credential is available only to the handoff step. Pull-request runs compile the code and have no upload credential.
