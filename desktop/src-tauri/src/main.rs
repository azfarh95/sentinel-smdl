// Prevents an extra console window on Windows release builds. The
// Tauri WebView is the only "window" we want — `windows_subsystem`
// suppresses the cmd-host that would otherwise pop up alongside.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    smdl_desktop_lib::run();
}
