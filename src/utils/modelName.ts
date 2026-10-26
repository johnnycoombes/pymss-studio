export function safeModelName(name: string): boolean {
  return Boolean(name) && name !== '.' && name !== '..'
    && !/[\s\u0000-\u001f/\\<>:"|?*]/.test(name)
    && !name.endsWith('.')
    && !/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$/i.test(name)
}
