/** What the connection check found for one tracker (`salmon checkconf -t`). */
export interface TrackerCheck {
  tracker: string
  ok: boolean
  cookie: 'ok' | 'failed' | 'not_checked'
  cookie_error: string | null
  key: 'ok' | 'failed' | 'not_set' | 'not_checked'
  key_error: string | null
  tls_error: string | null
  checked_at: string
}

/** The trackers a finished connection check job found, none if it did not finish. */
export function checksOf(result: unknown): TrackerCheck[] {
  return ((result as { trackers?: TrackerCheck[] } | null)?.trackers ?? []) as TrackerCheck[]
}

/** Why a tracker's check failed, a line per credential. */
export function problems(check: TrackerCheck): string[] {
  if (check.tls_error) return [check.tls_error]
  return [
    check.cookie_error ? `Session cookie: ${check.cookie_error}` : '',
    check.key_error ? `API key: ${check.key_error}` : '',
  ].filter(Boolean)
}
