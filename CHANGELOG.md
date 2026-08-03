# Changelog

## [0.1.9](https://github.com/jr200-labs/restic-backups/compare/v0.1.8...v0.1.9) (2026-08-03)


### Features

* restore GitHub repositories ([#40](https://github.com/jr200-labs/restic-backups/issues/40)) ([30d54b1](https://github.com/jr200-labs/restic-backups/commit/30d54b1430352c8837d5f72bf584b6708721b6a4))

## [0.1.8](https://github.com/jr200-labs/restic-backups/compare/v0.1.7...v0.1.8) (2026-08-03)


### Features

* add job logging and Prometheus metrics ([#38](https://github.com/jr200-labs/restic-backups/issues/38)) ([2a89d28](https://github.com/jr200-labs/restic-backups/commit/2a89d28bf288f19333b5cd4f6987f0219f0eebad))
* support disabled storage and unavailable jobs ([#37](https://github.com/jr200-labs/restic-backups/issues/37)) ([7683930](https://github.com/jr200-labs/restic-backups/commit/7683930d38395ca0b509f46168578885c1d44d7c))


### Bug Fixes

* **deps:** update all non-major dependencies ([#35](https://github.com/jr200-labs/restic-backups/issues/35)) ([f2e7915](https://github.com/jr200-labs/restic-backups/commit/f2e7915a69aa55bbec8a24f8a1b66e81c7386cd0))

## [0.1.7](https://github.com/jr200-labs/restic-backups/compare/v0.1.6...v0.1.7) (2026-08-02)


### Features

* add GitHub repository backups ([#30](https://github.com/jr200-labs/restic-backups/issues/30)) ([a22b662](https://github.com/jr200-labs/restic-backups/commit/a22b66203b3a1c99bd465897577dd2a3f40a3ff4))
* back up multiple GitHub repositories ([#33](https://github.com/jr200-labs/restic-backups/issues/33)) ([a96e278](https://github.com/jr200-labs/restic-backups/commit/a96e27834afa3838a57b8e6d3c791911bd1fc723))


### Code Refactoring

* unify backup jobs ([#32](https://github.com/jr200-labs/restic-backups/issues/32)) ([27bfd58](https://github.com/jr200-labs/restic-backups/commit/27bfd58559ede85900806b1fd60267526d136e8c))

## [0.1.6](https://github.com/jr200-labs/restic-backups/compare/v0.1.5...v0.1.6) (2026-08-02)


### Features

* support multi-repository backup jobs ([#28](https://github.com/jr200-labs/restic-backups/issues/28)) ([11f327f](https://github.com/jr200-labs/restic-backups/commit/11f327f355ef70257314f828b5f335475223c1c3))

## [0.1.5](https://github.com/jr200-labs/restic-backups/compare/v0.1.4...v0.1.5) (2026-08-02)


### Features

* add repository maintenance TUI ([#25](https://github.com/jr200-labs/restic-backups/issues/25)) ([d391c31](https://github.com/jr200-labs/restic-backups/commit/d391c31a0b9fcc218b24efb802a33ad2a7e6e69e))
* support local restic repositories ([#24](https://github.com/jr200-labs/restic-backups/issues/24)) ([880dba7](https://github.com/jr200-labs/restic-backups/commit/880dba72b86a0151c8c16f83387acb9fb344c0bd))


### Bug Fixes

* make TUI keyboard exits consistent ([#22](https://github.com/jr200-labs/restic-backups/issues/22)) ([9e47da6](https://github.com/jr200-labs/restic-backups/commit/9e47da66f238eda8f0d1643d329821eeb8b66531))
* validate only selected repository ([#27](https://github.com/jr200-labs/restic-backups/issues/27)) ([bc68648](https://github.com/jr200-labs/restic-backups/commit/bc6864889fa0baeac22284b5cc6f81b85024d38e))

## [0.1.4](https://github.com/jr200-labs/restic-backups/compare/v0.1.3...v0.1.4) (2026-08-02)


### Features

* add generic dry-run controls ([#17](https://github.com/jr200-labs/restic-backups/issues/17)) ([7c5cee5](https://github.com/jr200-labs/restic-backups/commit/7c5cee5c94230fbbc5b7e40d626ab0339923b350))

## [0.1.3](https://github.com/jr200-labs/restic-backups/compare/v0.1.2...v0.1.3) (2026-08-02)


### Features

* add configurable restic metadata caches ([#9](https://github.com/jr200-labs/restic-backups/issues/9)) ([ad613c5](https://github.com/jr200-labs/restic-backups/commit/ad613c592be204e96e7ff14772bbc3dbb8dae4f6))
* add contextual menu help ([#15](https://github.com/jr200-labs/restic-backups/issues/15)) ([4d66a1a](https://github.com/jr200-labs/restic-backups/commit/4d66a1a2e13aa011f1a10fdff40bd3a780f30c27))
* add navigable command menus ([eac934c](https://github.com/jr200-labs/restic-backups/commit/eac934c4e09f9cf79ccb86363d6e7e58a6a8a89d))
* group generic commands and list snapshots ([#11](https://github.com/jr200-labs/restic-backups/issues/11)) ([99124cb](https://github.com/jr200-labs/restic-backups/commit/99124cb7ca09f51e66bc5ea9b525b43590be741a))


### Bug Fixes

* **deps:** update all non-major dependencies ([#12](https://github.com/jr200-labs/restic-backups/issues/12)) ([59a2769](https://github.com/jr200-labs/restic-backups/commit/59a276916b48414e9bc905477a5f239e82e4c88b))
* improve interactive command safety ([#14](https://github.com/jr200-labs/restic-backups/issues/14)) ([4f18902](https://github.com/jr200-labs/restic-backups/commit/4f18902ca4b0d916fc15dacf298b27d33849b470))


### Reverts

* move direct changes to pull request ([2b5b74c](https://github.com/jr200-labs/restic-backups/commit/2b5b74c4ce48864142341157e3aa0b2e8b4106fb))


### Code Refactoring

* clarify backup job configuration ([#16](https://github.com/jr200-labs/restic-backups/issues/16)) ([d54dabe](https://github.com/jr200-labs/restic-backups/commit/d54dabe32a929a188f0e06da30d4fc47738763c4))
* rename backup IDs to job IDs ([23e64a3](https://github.com/jr200-labs/restic-backups/commit/23e64a32e17012d402f795b71e784c54b2088f19))

## [0.1.2](https://github.com/jr200-labs/restic-backups/compare/v0.1.1...v0.1.2) (2026-08-01)


### Bug Fixes

* resolve managed data beside config ([#6](https://github.com/jr200-labs/restic-backups/issues/6)) ([2a4b439](https://github.com/jr200-labs/restic-backups/commit/2a4b439e5aa118d49ec5d46c9e81da26d6b0b734))

## [0.1.1](https://github.com/jr200-labs/restic-backups/compare/v0.1.0...v0.1.1) (2026-08-01)


### Features

* initial commit ([49d5a63](https://github.com/jr200-labs/restic-backups/commit/49d5a6343d20c0ca06cfbe3664ebe1d6cd1bdb35))
