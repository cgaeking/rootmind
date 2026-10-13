fn main() {
    tauri_build::build();

    // tauri-build embeds the application manifest into the app binary only.
    // Test binaries need it too: with Tauri's mock runtime (backend.rs tests)
    // they link Common Controls v6 functions and would otherwise fail to start
    // with STATUS_ENTRYPOINT_NOT_FOUND.
    let msvc = std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") && msvc {
        let manifest = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("test-manifest.xml");
        println!("cargo:rerun-if-changed={}", manifest.display());
        println!("cargo:rustc-link-arg-tests=/MANIFEST:EMBED");
        println!(
            "cargo:rustc-link-arg-tests=/MANIFESTINPUT:{}",
            manifest.display()
        );
    }
}
