// Tauri pipes Cargo/app stderr. Capture the launching terminal's color capability
// before that pipe is created, while respecting explicit color preferences.
const command = process.argv.slice(2).find(arg => !arg.startsWith('-'))
if (command === 'dev' && process.stderr.isTTY
  && process.env.NO_COLOR === undefined && process.env.FORCE_COLOR === undefined
  && process.env.PYMSS_STUDIO_TERMINAL_COLOR === undefined) {
  // A private hint avoids forcing ANSI in Python/pip subprocesses and their log files.
  process.env.PYMSS_STUDIO_TERMINAL_COLOR = '1'
}

// Use the official entry point for argument handling, errors, and process lifetime.
await import('@tauri-apps/cli/tauri.js')
