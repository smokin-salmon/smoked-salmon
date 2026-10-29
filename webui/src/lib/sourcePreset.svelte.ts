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
export function sourcePreset(path: () => string, set: (source: string) => void) {
  let reason = $state('')
  // The folder the select's current source belongs to (preset for it, or picked by the user while it was open).
  let owner: string | null = null
  // A source picked while no folder was entered, not yet claimed by a folder.
  let early = false

  $effect(() => {
    const folder = path().trim()
    // Another folder: the source of the last one does not carry over.
    if (owner !== null && owner !== folder) {
      set('')
      owner = null
      reason = ''
    }
    if (!folder) return
    let stale = false
    const timer = setTimeout(() => {
      apiGet<Found>(`/source?path=${encodeURIComponent(folder)}`)
        .then((found) => {
          if (stale) return
          if (early && owner === null) {
            // The user chose a source before any folder: it stays, and no preset replaces it.
            owner = folder
            early = false
            reason = ''
            return
          }
          if (found.source) {
            if (owner !== folder) {
              owner = folder
              set(found.source)
              reason = found.reason ?? ''
            }
          } else if (owner !== folder) {
            // Nothing found: the select stays on "Pick one", or on what the user chose before.
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
      owner = path().trim() || null
      // Picked before any folder: it is the pick for the first folder that is looked up.
      early = owner === null
      reason = ''
    },
  }
}
