# Pinned firmware dependencies

| Component | Version | Commit | License |
|---|---|---|---|
| STMicroelectronics `cmsis-device-c0` | v1.4.1 | `bcc8d94fecb767d1afb53d8c12a8f87ebeb503a2` | Apache-2.0; see submodule `LICENSE.md` |
| Arm `CMSIS_5` Core | 5.9.0 | `2b7495b8535bdcb306dac29b9ded4cfb679d7e5c` | Apache-2.0; see submodule `LICENSE.txt` |

Both dependencies are Git submodules. Initialize them with:

```sh
git submodule update --init --recursive
```

Target builds require no network access after the submodules and Debian ARM
cross-toolchain have been fetched.
