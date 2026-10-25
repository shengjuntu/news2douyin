# V7 implementation notes

## 0.1.1 reliability update

See [release-0.1.1.md](docs/release-0.1.1.md) for corrected event links,
transaction rollback, durable UTC schedule claims, shared search/filtering,
environment configuration, optional dependencies, packaged resources and the
preview-first historical repair utility. The remaining queue/video limitations
are listed explicitly there.

This package adds a server-first V7 architecture on top of the existing `news2douyin_simplified` codebase.

## Implemented
- Long-running FastAPI server under `src/news2douyin/server/`
- SQLite-backed persistence under `src/news2douyin/storage/`
- Profile and job configuration model
- Manual run-now collection flow
- Scheduler service for daily timed collection
- Stronger de-dup pipeline under `src/news2douyin/dedup/`
- Search services for articles and events
- Decoupled editorial and video-script package generation
- New CLI commands:
  - `news2douyin v7-init`
  - `news2douyin v7-serve`
  - `news2douyin v7-run-now`
  - `news2douyin v7-search-articles`
  - `news2douyin v7-search-events`
  - `news2douyin v7-build-script`
  - `news2douyin-server`
- Example configs under `configs/v7/`

## Main directories
- `src/news2douyin/server/`
- `src/news2douyin/scheduler/`
- `src/news2douyin/collect/`
- `src/news2douyin/dedup/`
- `src/news2douyin/enrich/`
- `src/news2douyin/storage/`
- `src/news2douyin/search/`
- `src/news2douyin/editorial/`
- `src/news2douyin/video/`

## Notes
- The provider layer currently supports `worldnewsapi` and a `mock` provider for smoke tests.
- Category filtering is implemented as an internal classification/filter layer so that future providers can be added without changing the profile schema.
- The legacy platform entry point was turned into a compatibility shim that now points to the V7 FastAPI app.
- Existing legacy v6 commands and code paths were preserved.

## Smoke-tested
Using the `mock_market` profile:
1. init db
2. import profile
3. run collection
4. search events
5. build script package

## Not yet migrated
- Existing PyQt GUI is not yet fully switched to REST-only V7 endpoints.
- Full-text FTS index is not yet added; current search is a simple persisted query layer.
- Auto-editorial / auto-video flags are stored on jobs but not yet chained automatically after scheduler runs.


## REST client desktop UI

This update adds a new PyQt desktop client for the V7 server. It connects to the FastAPI server over REST and supports:

- server health / scheduler status
- profile create/update
- job create/update and enable/disable
- manual run-now with override parameters
- recent runs browser
- event/article search
- editorial build and script package build
- open generated package directory

Entry point:

```bash
news2douyin-v7-gui
```
