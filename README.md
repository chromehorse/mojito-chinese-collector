# Chinese publisher collector

The manual workflow checks public HTML and JSON sources for the Ministry of Ecology and Environment, Shanghai AI Lab, and the Ministry of Industry and Information Technology. It runs in a temporary Linux container with an unprivileged account. Public logs contain per-run counts and status. Files containing source content stay out of the public repository and Actions artifacts.

Choose **Send validated text to private intake** when dispatching the workflow to test the storage handoff. The `private-intake` environment holds the repository-scoped `INTAKE_UPLOAD_TOKEN`. Configure the environment to accept the default branch only. The destination must be an initialized private `mojito-chinese-intake` repository. The write credential is available only to the handoff step. Pull-request runs compile the code and have no upload credential.
