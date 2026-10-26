import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const mode = process.argv[2] || 'dev'
if (!['dev', 'build'].includes(mode)) throw new Error('Usage: node scripts/vst-lab.mjs dev|build')
const dataRoot = path.join(root, 'data-vst-lab')
const env = {
  ...process.env,
  PYMSS_STUDIO_DATA_ROOT: dataRoot,
  PYMSS_STUDIO_DEFAULT_OUTPUT_DIR: path.join(dataRoot, 'outputs'),
  PYMSS_STUDIO_RUNTIME_ENVS_DIR: path.join(dataRoot, 'runtime-envs'),
  PYMSS_STUDIO_ACTIVE_RUNTIME_FILE: path.join(dataRoot, 'runtime-envs', 'active-runtime.json'),
}
const args = [path.join(root, 'scripts', 'tauri.mjs'), mode,
  '--config', 'src-tauri/tauri.vstlab.conf.json', '--features', 'vst-lab']
if (mode === 'build') args.push('--debug', '--no-bundle')
args.push(...process.argv.slice(3))
const child = spawn(process.execPath, args, { cwd: root, env, stdio: 'inherit' })
child.on('error', error => { console.error(error.message); process.exitCode = 1 })
child.on('exit', (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0) })
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => child.kill(signal))
