import { apiGet } from './api'

interface Found {
  source: string | null
  reason: string | null
}

const DEBOUNCE_MS = 400

/**
 * The Source select's preset for the folder in `path()`: what the files prove, else the folder name's bracket.
 * Reads local files only. Never remembered between uploads; a source the user picked for this folder is kept.
 */
export function sourcePreset(path: () => string, current: () => string, set: (source: string) => void) {
  let reason = $state('')
  // The source this lookup put in the select, and the folder the user picked a source for.
  let preset = ''
  let pickedFor = ''

  $effect(() => {
    const folder = path().trim()
    if (!folder) {
      reason = ''
      return
    }
    let stale = false
    const timer = setTimeout(() => {
      apiGet<Found>(`/source?path=${encodeURIComponent(folder)}`)
        .then((found) => {
          if (stale) return
          if (found.source) {
            if (pickedFor !== folder) {
              preset = found.source
              set(found.source)
              reason = found.reason ?? ''
            }
          } else {
            // Nothing found: back to "Pick one", unless the user chose.
            if (preset && current() === preset) set('')
            preset = ''
            reason = ''
          }
        })
        .catch(() => {
          // Not a folder salmon may read yet (a half-typed path): no preset, and nothing to say.
          if (!stale) reason = ''
        })
    }, DEBOUNCE_MS)
    return () => {
      stale = true
      clearTimeout(timer)
    }
  })

  return {
    get reason() {
      return reason
    },
    /** The user chose a source: this folder's preset no longer applies. */
    picked() {
      pickedFor = path().trim()
      preset = ''
      reason = ''
    },
  }
}
