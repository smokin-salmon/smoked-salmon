/**
 * Copy text to the clipboard of this browser. The server's clipboard is nobody's: the jobs never touch it.
 *
 * `navigator.clipboard` exists only on a page served over HTTPS or from localhost; salmon web may be reached over
 * plain HTTP on a LAN, so a hidden textarea and `execCommand` stand in for it. Returns whether the copy worked.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Refused (no permission, no focus): try the other way.
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  try {
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(area)
  }
}
