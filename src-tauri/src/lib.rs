mod sidecar;

use serde::Serialize;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::net::IpAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::ipc::{CapabilityBuilder, InvokeBody, Request};
use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Manager, RunEvent, State, Url, Webview, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

use crate::sidecar::{
    spawn_and_verify_with_registration, spawn_unexpected_exit_monitor, SidecarController,
    SidecarError, STARTUP_TIMEOUT,
};

const MAX_EXPORT_BYTES: usize = 550 * 1024 * 1024;
const MAX_EXTERNAL_URL_LEN: usize = 2048;

#[derive(Clone)]
struct ActiveRuntime {
    port: u16,
    launch_token: String,
}

#[derive(Default)]
struct DesktopState {
    controller: Mutex<Option<Arc<SidecarController>>>,
    runtime: Mutex<Option<ActiveRuntime>>,
}

impl DesktopState {
    fn set_controller(&self, ctrl: Arc<SidecarController>) {
        let mut guard = self.controller.lock().unwrap_or_else(|e| e.into_inner());
        *guard = Some(ctrl);
    }

    fn set_runtime(&self, port: u16, launch_token: String) {
        let mut guard = self.runtime.lock().unwrap_or_else(|e| e.into_inner());
        *guard = Some(ActiveRuntime { port, launch_token });
    }

    fn get_runtime(&self) -> Option<ActiveRuntime> {
        self.runtime
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    fn shutdown_sidecar(&self, graceful_timeout: Duration) {
        if let Ok(mut rt_guard) = self.runtime.lock() {
            *rt_guard = None;
        }
        let ctrl_opt = self
            .controller
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take();
        if let Some(ctrl) = ctrl_opt {
            ctrl.shutdown_blocking(graceful_timeout);
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeContextDto {
    mode: &'static str,
    api_base_url: String,
    port: u16,
    launch_token: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct SaveFileResult {
    canceled: bool,
    saved: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    path: Option<String>,
}

fn is_allowed_main_navigation(url: &Url, expected_port: u16) -> bool {
    url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port() == Some(expected_port)
}

fn is_allowed_splash_navigation(url: &Url) -> bool {
    // macOS/Linux (WKWebView/WebKitGTK) serve WebviewUrl::App on tauri://localhost;
    // Windows (WebView2) serves WebviewUrl::App on http://tauri.localhost or https://tauri.localhost.
    if url.scheme() == "tauri" {
        return true;
    }
    matches!(url.scheme(), "http" | "https") && url.host_str() == Some("tauri.localhost")
}

fn verify_main_webview_origin(webview: &Webview, expected_port: u16) -> Result<(), String> {
    if webview.label() != "main" || webview.window().label() != "main" {
        return Err("IPC command restricted to primary application window.".into());
    }
    let current_url = webview
        .url()
        .map_err(|_| "Unable to determine caller origin.".to_string())?;
    if !is_allowed_main_navigation(&current_url, expected_port) {
        return Err("IPC command denied for untrusted webview origin.".into());
    }
    Ok(())
}

fn percent_decode_utf8(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let (Some(h), Some(l)) = (
                (bytes[i + 1] as char).to_digit(16),
                (bytes[i + 2] as char).to_digit(16),
            ) {
                out.push(((h << 4) | l) as u8);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn sanitize_export_filename(raw: &str) -> String {
    let decoded = percent_decode_utf8(raw.trim());
    let segment = decoded
        .rsplit(|c| c == '/' || c == '\\')
        .find(|s| !s.trim().is_empty())
        .unwrap_or("")
        .trim();

    let mut cleaned: String = segment
        .chars()
        .map(|c| {
            if c.is_control() || matches!(c, '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*') {
                '_'
            } else {
                c
            }
        })
        .collect();

    cleaned = cleaned.trim_start_matches('.').trim().to_string();
    if cleaned.is_empty() || cleaned == "." || cleaned == ".." {
        return "krushidhan_export.bin".into();
    }

    if cleaned.chars().count() > 160 {
        cleaned = cleaned.chars().take(160).collect();
    }
    cleaned
}

fn infer_dialog_filter(filename: &str, mime_type: &str) -> Option<(&'static str, &'static [&'static str])> {
    let lower_name = filename.to_ascii_lowercase();
    let lower_mime = mime_type.to_ascii_lowercase();
    if lower_name.ends_with(".kdb.enc") {
        Some(("Krushidhan Encrypted Backup", &["enc"]))
    } else if lower_name.ends_with(".pdf") || lower_mime.contains("pdf") {
        Some(("PDF Document", &["pdf"]))
    } else if lower_name.ends_with(".xlsx") || lower_mime.contains("spreadsheetml") {
        Some(("Excel Workbook", &["xlsx"]))
    } else if lower_name.ends_with(".db") || lower_mime.contains("sqlite") {
        Some(("SQLite Database Backup", &["db"]))
    } else {
        None
    }
}

fn validate_external_url(raw: &str) -> Result<Url, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err("External URL cannot be empty.".into());
    }
    if trimmed.len() > MAX_EXTERNAL_URL_LEN || trimmed.chars().any(|c| c.is_control()) {
        return Err("External URL rejected due to unsafe characters or length.".into());
    }

    let parsed = Url::parse(trimmed).map_err(|_| "Invalid external URL format.".to_string())?;
    if parsed.scheme() != "https" && parsed.scheme() != "http" {
        return Err("Only http:// and https:// external URLs are permitted.".into());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("External URLs with embedded credentials are not permitted.".into());
    }

    let Some(host) = parsed.host_str() else {
        return Err("External URL must include a valid host.".into());
    };
    let lower_host = host.trim_matches(['[', ']']).to_ascii_lowercase();
    if lower_host == "localhost" || lower_host.ends_with(".localhost") {
        return Err("Localhost URLs cannot be opened via external link bridge.".into());
    }
    if let Ok(ip) = lower_host.parse::<IpAddr>() {
        if ip.is_loopback() || ip.is_unspecified() {
            return Err("Loopback URLs cannot be opened via external link bridge.".into());
        }
    }

    Ok(parsed)
}

fn extract_export_bytes(body: &InvokeBody) -> Result<Vec<u8>, String> {
    match body {
        InvokeBody::Raw(bytes) => {
            if bytes.len() > MAX_EXPORT_BYTES {
                return Err("Export payload exceeds maximum allowed desktop file size.".into());
            }
            Ok(bytes.clone())
        }
        InvokeBody::Json(val) => {
            let arr = if let Some(arr) = val.as_array() {
                arr
            } else if let Some(arr) = val.get("bytes").and_then(|b| b.as_array()) {
                arr
            } else {
                return Err("Expected binary byte payload for desktop file save.".into());
            };
            if arr.len() > MAX_EXPORT_BYTES {
                return Err("Export payload exceeds maximum allowed desktop file size.".into());
            }
            let mut out = Vec::with_capacity(arr.len());
            for item in arr {
                let n = item
                    .as_u64()
                    .and_then(|v| u8::try_from(v).ok())
                    .ok_or_else(|| "Invalid byte element in export payload.".to_string())?;
                out.push(n);
            }
            Ok(out)
        }
    }
}

#[tauri::command]
fn get_sidecar_runtime(
    webview: Webview,
    state: State<'_, DesktopState>,
) -> Result<RuntimeContextDto, String> {
    let runtime = state
        .get_runtime()
        .ok_or_else(|| "Sidecar runtime is not initialized.".to_string())?;
    verify_main_webview_origin(&webview, runtime.port)?;

    Ok(RuntimeContextDto {
        mode: "desktop",
        api_base_url: format!("http://127.0.0.1:{}", runtime.port),
        port: runtime.port,
        launch_token: runtime.launch_token,
    })
}

#[tauri::command]
async fn save_exported_file(
    app: AppHandle,
    webview: Webview,
    state: State<'_, DesktopState>,
    request: Request<'_>,
) -> Result<SaveFileResult, String> {
    let runtime = state
        .get_runtime()
        .ok_or_else(|| "Sidecar runtime is not initialized.".to_string())?;
    verify_main_webview_origin(&webview, runtime.port)?;

    let raw_filename = request
        .headers()
        .get("x-krushidhan-filename")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("krushidhan_export.bin");
    let safe_filename = sanitize_export_filename(raw_filename);

    let mime_type = request
        .headers()
        .get("x-krushidhan-mime")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/octet-stream")
        .to_string();

    let payload_bytes = extract_export_bytes(request.body())?;
    let parent_window = webview.window();

    let mut builder = app
        .dialog()
        .file()
        .set_parent(&parent_window)
        .set_title("Save File")
        .set_file_name(&safe_filename);

    if let Some((filter_name, exts)) = infer_dialog_filter(&safe_filename, &mime_type) {
        builder = builder.add_filter(filter_name, exts);
    }

    let chosen = builder.blocking_save_file();
    let Some(selected_path) = chosen else {
        return Ok(SaveFileResult {
            canceled: true,
            saved: false,
            path: None,
        });
    };

    let dest_path = selected_path
        .into_path()
        .map_err(|err| format!("Could not resolve selected save location: {err}"))?;

    let mut file: File = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&dest_path)
        .map_err(|err| format!("Failed opening destination file {}: {err}", dest_path.display()))?;

    file.write_all(&payload_bytes)
        .map_err(|err| format!("Failed writing exported file {}: {err}", dest_path.display()))?;
    file.sync_all()
        .map_err(|err| format!("Failed flushing exported file {}: {err}", dest_path.display()))?;

    Ok(SaveFileResult {
        canceled: false,
        saved: true,
        path: Some(dest_path.to_string_lossy().into_owned()),
    })
}

#[derive(Serialize)]
struct PickFolderResult {
    canceled: bool,
    path: Option<String>,
}

#[tauri::command]
async fn pick_folder_path(
    app: AppHandle,
    webview: Webview,
    state: State<'_, DesktopState>,
    title: Option<String>,
) -> Result<PickFolderResult, String> {
    let runtime = state
        .get_runtime()
        .ok_or_else(|| "Sidecar runtime is not initialized.".to_string())?;
    verify_main_webview_origin(&webview, runtime.port)?;

    let parent_window = webview.window();
    let dialog_title = title
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("Select Synchronized Backup Folder");

    let chosen = app
        .dialog()
        .file()
        .set_parent(&parent_window)
        .set_title(dialog_title)
        .blocking_pick_folder();

    let Some(selected) = chosen else {
        return Ok(PickFolderResult {
            canceled: true,
            path: None,
        });
    };

    let dest_path = selected
        .into_path()
        .map_err(|err| format!("Could not resolve selected folder path: {err}"))?;

    Ok(PickFolderResult {
        canceled: false,
        path: Some(dest_path.to_string_lossy().into_owned()),
    })
}

#[tauri::command]
fn open_external_link(
    app: AppHandle,
    webview: Webview,
    state: State<'_, DesktopState>,
    url: String,
) -> Result<(), String> {
    let runtime = state
        .get_runtime()
        .ok_or_else(|| "Sidecar runtime is not initialized.".to_string())?;
    verify_main_webview_origin(&webview, runtime.port)?;

    let validated = validate_external_url(&url)?;
    app.opener()
        .open_url(validated.as_str(), None::<&str>)
        .map_err(|err| format!("Could not open external browser link: {err}"))
}

fn build_desktop_init_script(port: u16) -> String {
    let expected_origin = format!("http://127.0.0.1:{port}");
    format!(
        r##"(function() {{
  var expectedOrigin = {expected_origin_json};
  if (typeof window === "undefined" || window.location.origin !== expectedOrigin) {{
    return;
  }}
  if (window.__KRUSHIDHAN_DESKTOP__ && window.__KRUSHIDHAN_DESKTOP__.mode === "desktop") {{
    return;
  }}
  function invokeTauri(cmd, payload, options) {{
    var inv = (window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke) ||
              (window.__TAURI_INTERNALS__ && window.__TAURI_INTERNALS__.invoke);
    if (typeof inv !== "function") {{
      return Promise.reject(new Error("Desktop IPC bridge unavailable"));
    }}
    return inv(cmd, payload, options);
  }}
  async function saveFile(opts) {{
    var cfg = opts || {{}};
    var filename = String(cfg.filename || "krushidhan_export.bin");
    var mimeType = String(cfg.mimeType || "application/octet-stream");
    var rawBytes = cfg.bytes;
    if (!rawBytes && cfg.blob && typeof cfg.blob.arrayBuffer === "function") {{
      rawBytes = new Uint8Array(await cfg.blob.arrayBuffer());
    }}
    if (rawBytes instanceof ArrayBuffer) {{
      rawBytes = new Uint8Array(rawBytes);
    }}
    if (!(rawBytes instanceof Uint8Array)) {{
      throw new Error("Missing binary payload for desktop saveFile");
    }}
    return await invokeTauri("save_exported_file", rawBytes, {{
      headers: {{
        "X-Krushidhan-Filename": encodeURIComponent(filename),
        "X-Krushidhan-Mime": mimeType
      }}
    }});
  }}
  async function openExternal(url) {{
    var cleanUrl = String(url || "").trim();
    if (!cleanUrl) {{
      throw new Error("External URL is empty");
    }}
    return await invokeTauri("open_external_link", {{ url: cleanUrl }});
  }}
  async function pickFolder(title) {{
    return await invokeTauri("pick_folder_path", {{ title: title || "Select Synchronized Backup Folder" }});
  }}
  var ctx = {{
    mode: "desktop",
    apiBaseUrl: expectedOrigin,
    saveFile: saveFile,
    openExternal: openExternal,
    pickFolder: pickFolder
  }};
  try {{
    Object.defineProperty(window, "__KRUSHIDHAN_DESKTOP__", {{
      value: Object.freeze(ctx),
      writable: false,
      configurable: false
    }});
  }} catch (e) {{
    window.__KRUSHIDHAN_DESKTOP__ = ctx;
  }}
}})();"##,
        expected_origin_json = serde_json::to_string(&expected_origin)
            .expect("origin serialization cannot fail")
    )
}

fn show_startup_error_and_exit(app_handle: &AppHandle, err: &SidecarError) {
    let title = if err.is_single_instance() {
        "Krushidhan ERP — Already Running"
    } else {
        "Krushidhan ERP — Startup Failed"
    };
    let exit_code = if err.is_single_instance() { 2 } else { 1 };

    let mut dialog = app_handle
        .dialog()
        .message(err.user_message())
        .title(title)
        .kind(MessageDialogKind::Error);
    if let Some(splash) = app_handle.get_webview_window("splash") {
        dialog = dialog.parent(&splash);
    }
    dialog.blocking_show();

    if let Some(splash) = app_handle.get_webview_window("splash") {
        let _ = splash.close();
    }
    app_handle.exit(exit_code);
}

fn start_sidecar_and_open_main(app_handle: AppHandle) {
    let binary_path = match sidecar::resolve_sidecar_binary() {
        Ok(p) => p,
        Err(err) => {
            show_startup_error_and_exit(&app_handle, &err);
            return;
        }
    };

    let mut cmd = std::process::Command::new(&binary_path);
    cmd.env("PYTHONUNBUFFERED", "1");
    cmd.env_remove("KRUSHIDHAN_DEV_ALLOW_MISSING_LAUNCH_TOKEN");

    let handle_for_reg = app_handle.clone();
    let spawn_result = spawn_and_verify_with_registration(cmd, STARTUP_TIMEOUT, move |ctrl| {
        handle_for_reg.state::<DesktopState>().set_controller(ctrl);
    });

    let (controller, handshake) = match spawn_result {
        Ok(pair) => pair,
        Err(err) => {
            let shutting_down = app_handle
                .state::<DesktopState>()
                .controller
                .lock()
                .ok()
                .and_then(|g| g.as_ref().map(|c| c.is_shutting_down()))
                .unwrap_or(false);
            if !shutting_down {
                show_startup_error_and_exit(&app_handle, &err);
            }
            return;
        }
    };

    if controller.is_shutting_down() {
        return;
    }

    let port = handshake.port;
    app_handle
        .state::<DesktopState>()
        .set_runtime(port, handshake.launch_token.clone());

    let origin_pattern = format!("http://127.0.0.1:{port}/*");
    if let Err(err) = app_handle.add_capability(
        CapabilityBuilder::new("krushidhan-main-erp")
            .window("main")
            .local(false)
            .remote(origin_pattern)
            .permission("allow-get-sidecar-runtime")
            .permission("allow-save-exported-file")
            .permission("allow-pick-folder-path")
            .permission("allow-open-external-link"),
    ) {
        let state = app_handle.state::<DesktopState>();
        state.shutdown_sidecar(Duration::from_secs(3));
        show_startup_error_and_exit(
            &app_handle,
            &SidecarError::StartupFailed(format!(
                "Failed to register desktop capability policy: {err}"
            )),
        );
        return;
    }

    let main_url: Url = match format!("http://127.0.0.1:{port}/").parse() {
        Ok(u) => u,
        Err(err) => {
            app_handle
                .state::<DesktopState>()
                .shutdown_sidecar(Duration::from_secs(3));
            show_startup_error_and_exit(
                &app_handle,
                &SidecarError::StartupFailed(format!("Invalid local sidecar URL: {err}")),
            );
            return;
        }
    };

    let init_script = build_desktop_init_script(port);
    let main_window = match WebviewWindowBuilder::new(
        &app_handle,
        "main",
        WebviewUrl::External(main_url),
    )
    .title("Krushidhan ERP")
    .inner_size(1360.0, 860.0)
    .min_inner_size(1024.0, 680.0)
    .center()
    .focused(true)
    .on_navigation(move |url| is_allowed_main_navigation(url, port))
    .on_new_window(|_url, _features| NewWindowResponse::Deny)
    .on_download(|_webview, _event| false)
    .initialization_script(&init_script)
    .build()
    {
        Ok(win) => win,
        Err(err) => {
            app_handle
                .state::<DesktopState>()
                .shutdown_sidecar(Duration::from_secs(3));
            show_startup_error_and_exit(
                &app_handle,
                &SidecarError::StartupFailed(format!(
                    "Failed to create main Krushidhan ERP desktop window: {err}"
                )),
            );
            return;
        }
    };

    // Open main window cleanly before closing splash so app stays alive through handoff.
    let _ = main_window.show();
    let _ = main_window.set_focus();
    if let Some(splash) = app_handle.get_webview_window("splash") {
        let _ = splash.close();
    }

    let monitor_app = app_handle.clone();
    spawn_unexpected_exit_monitor(controller, move |status, tail| {
        let code_desc = status
            .and_then(|s| s.code())
            .map(|c| format!(" (exit code {c})"))
            .unwrap_or_default();
        let detail = if tail.trim().is_empty() {
            String::new()
        } else {
            format!("\n\nRecent backend diagnostics:\n{tail}")
        };
        let msg = format!(
            "The local Krushidhan ERP backend service stopped unexpectedly{code_desc}.\n\nThe application will now close to protect your local shop database.{detail}"
        );

        let mut dialog = monitor_app
            .dialog()
            .message(&msg)
            .title("Krushidhan ERP — Backend Stopped")
            .kind(MessageDialogKind::Error);
        if let Some(win) = monitor_app.get_webview_window("main") {
            dialog = dialog.parent(&win);
        }
        dialog.blocking_show();

        monitor_app
            .state::<DesktopState>()
            .shutdown_sidecar(Duration::from_secs(2));
        monitor_app.exit(1);
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .manage(DesktopState::default())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::Builder::new().open_js_links_on_click(false).build())
        .invoke_handler(tauri::generate_handler![
            get_sidecar_runtime,
            save_exported_file,
            pick_folder_path,
            open_external_link
        ])
        .setup(|app| {
            let splash = WebviewWindowBuilder::new(
                app,
                "splash",
                WebviewUrl::App("index.html".into()),
            )
            .title("Krushidhan ERP")
            .inner_size(460.0, 290.0)
            .resizable(false)
            .center()
            .on_navigation(|url| is_allowed_splash_navigation(url))
            .on_new_window(|_url, _features| NewWindowResponse::Deny)
            .build()?;
            let _ = splash.show();

            let handle = app.handle().clone();
            std::thread::Builder::new()
                .name("krushidhan-startup".into())
                .spawn(move || start_sidecar_and_open_main(handle))
                .map_err(|err| Box::new(err) as Box<dyn std::error::Error>)?;

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Krushidhan ERP desktop shell");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            app_handle
                .state::<DesktopState>()
                .shutdown_sidecar(sidecar::GRACEFUL_SHUTDOWN_TIMEOUT);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_allowed_main_navigation_strict_origin_lock() {
        let port = 55123;
        assert!(is_allowed_main_navigation(
            &Url::parse("http://127.0.0.1:55123/").unwrap(),
            port
        ));
        assert!(is_allowed_main_navigation(
            &Url::parse("http://127.0.0.1:55123/static/js/app.js?v=1").unwrap(),
            port
        ));

        // Reject different port, localhost alias, https, external origins, or file schemes
        assert!(!is_allowed_main_navigation(
            &Url::parse("http://127.0.0.1:8008/").unwrap(),
            port
        ));
        assert!(!is_allowed_main_navigation(
            &Url::parse("http://localhost:55123/").unwrap(),
            port
        ));
        assert!(!is_allowed_main_navigation(
            &Url::parse("https://127.0.0.1:55123/").unwrap(),
            port
        ));
        assert!(!is_allowed_main_navigation(
            &Url::parse("https://example.com/").unwrap(),
            port
        ));
        assert!(!is_allowed_main_navigation(
            &Url::parse("file:///etc/passwd").unwrap(),
            port
        ));
    }

    #[test]
    fn test_validate_external_url_allows_only_safe_http_https() {
        assert!(validate_external_url("https://wa.me/919999999999?text=hi").is_ok());
        assert!(validate_external_url("http://example.com/help").is_ok());

        assert!(validate_external_url("file:///etc/passwd").is_err());
        assert!(validate_external_url("javascript:alert(1)").is_err());
        assert!(validate_external_url("data:text/html,hi").is_err());
        assert!(validate_external_url("http://127.0.0.1:55123/api/health").is_err());
        assert!(validate_external_url("http://localhost:8008/").is_err());
        assert!(validate_external_url("https://user:pass@example.com/").is_err());
    }

    #[test]
    fn test_sanitize_export_filename_strips_traversal_and_unsafe_chars() {
        assert_eq!(
            sanitize_export_filename("../../evil/Invoice_INV-001.pdf"),
            "Invoice_INV-001.pdf"
        );
        assert_eq!(
            sanitize_export_filename("..\\..\\Product%20Catalog%20(2026).xlsx"),
            "Product Catalog (2026).xlsx"
        );
        assert_eq!(
            sanitize_export_filename("..."),
            "krushidhan_export.bin"
        );
    }

    #[test]
    fn test_infer_dialog_filter_matches_expected_types() {
        assert_eq!(
            infer_dialog_filter("Invoice_1.pdf", "application/pdf"),
            Some(("PDF Document", &["pdf"][..]))
        );
        assert_eq!(
            infer_dialog_filter(
                "Sales_Register.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            Some(("Excel Workbook", &["xlsx"][..]))
        );
        assert_eq!(
            infer_dialog_filter("shop_backup.db", "application/vnd.sqlite3"),
            Some(("SQLite Database Backup", &["db"][..]))
        );
        assert_eq!(
            infer_dialog_filter("shop_backup.kdb.enc", "application/octet-stream"),
            Some(("Krushidhan Encrypted Backup", &["enc"][..]))
        );
    }

    #[test]
    fn test_is_allowed_splash_navigation_supports_windows_and_macos() {
        assert!(is_allowed_splash_navigation(&Url::parse("tauri://localhost/index.html").unwrap()));
        assert!(is_allowed_splash_navigation(&Url::parse("http://tauri.localhost/index.html").unwrap()));
        assert!(is_allowed_splash_navigation(&Url::parse("https://tauri.localhost/index.html").unwrap()));

        assert!(!is_allowed_splash_navigation(&Url::parse("https://example.com/").unwrap()));
        assert!(!is_allowed_splash_navigation(&Url::parse("http://127.0.0.1:8008/").unwrap()));
        assert!(!is_allowed_splash_navigation(&Url::parse("file:///C:/Windows/win.ini").unwrap()));
    }
}
