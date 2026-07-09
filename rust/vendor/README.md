# Vendored: demoparser2 Rust core

`parser/` and `csgoproto/` are vendored verbatim from
https://github.com/LaihoE/demoparser at tag **v0.41.3** (commit `54e320f381`) —
the exact release our Python venv's `demoparser2==0.41.3` wheel is built from
(MIT licensed upstream).

Local modifications (build hygiene only, no source changes):
- deleted both `build.rs` files: `csgoproto/build.rs` git-clones GameTracking-CS2
  and runs protoc at build time to REGENERATE the checked-in protobuf code; all of
  its output (`csgoproto/src/protobuf.rs`, `maps.rs`, `message_type.rs`) is already
  committed upstream and compiles as-is. Deleting the scripts makes our build
  hermetic (no protoc, no network) and pins the protos to the v0.41.3 code.
- deleted `parser/test_demo.dem` (60 MB test fixture) and `parser/Cargo.lock`.
- removed `parser/Cargo.toml`'s `[profile.*]` sections (ignored inside our workspace;
  cargo warned on every build — the workspace root pins these crates to opt-level 3).

There is no crates.io release of this parser (the core is an unpublished workspace
member), which is why it is vendored rather than declared as a registry dependency.
To upgrade: re-clone at the new tag, re-apply the deletions above, and re-run the
extraction cross-verification against the Python pipeline.
