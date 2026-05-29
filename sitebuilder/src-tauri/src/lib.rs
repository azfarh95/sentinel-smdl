// Sentinel Sitebuilder desktop entry — a thin Tauri 2 wrap around the remote
// theme-token editor at https://media.az-sentinel.xyz/app/sitebuilder.
//
// The window URL is set declaratively in tauri.conf.json (windows[0].url),
// so the runtime path is intentionally minimal: build the Tauri runtime
// with default config + an optional log plugin in debug builds.

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Sentinel Sitebuilder desktop");
}
