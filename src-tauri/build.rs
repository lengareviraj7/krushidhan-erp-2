fn main() {
    let attributes = tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "get_sidecar_runtime",
            "save_exported_file",
            "pick_folder_path",
            "open_external_link",
        ]),
    );
    tauri_build::try_build(attributes).expect("failed to run tauri-build");
}
